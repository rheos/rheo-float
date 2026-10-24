"""``finance.rate.fetch``, the ``rates`` sink spec and its preview (FR-14, FR-65,
AC-15, AC-78).

Seams: ``operations.dispatch`` of ``finance.rate.fetch`` and ``finance.sink.preview``
against a stub Valet server, with a ledger derived from ``FX_LINES`` documents; the
gate (``sinks.gate.is_enabled``); and ``finance rate fetch`` through ``cli.main``. An
approval is a fixture row pinned to the provider's current spec. Every date, amount
and rate is invented; the series ids are Valet's.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Callable
from decimal import Decimal
from typing import TYPE_CHECKING
from urllib.parse import parse_qsl, urlsplit

import pytest
from sqlalchemy import select

from derive.support import Ledger, fx_lines
from rheo_finance import catalogue, composition, operations
from rheo_finance.cli.main import main
from rheo_finance.contracts import Role
from rheo_finance.rates import valet
from rheo_finance.rates.valet import FetchOut
from rheo_finance.sinks.gate import is_enabled
from rheo_finance.sinks.ops import Preview
from rheo_finance.sinks.spec import spec_digest
from rheo_finance.storage.schema import rate, txn

from .conftest import valet_reply

if TYPE_CHECKING:
    from conftest import StubServer

catalogue.load_all()

T0 = dt.datetime(2026, 1, 10, 9, 0, tzinfo=dt.UTC)
USD_LINE = "2025-12-10 -10.00 - - SAMPLE SHOP 99951"
EUR_LINE = "2025-12-11 -20.00 - - SAMPLE SHOP 99952"
Approve = Callable[..., uuid.UUID]


def fetch(ledger: Ledger, **payload: object) -> FetchOut:
    out = operations.dispatch("finance.rate.fetch", payload, Role.OWNER, ctx=ledger.ctx)
    assert isinstance(out, FetchOut)
    return out


def preview(ledger: Ledger) -> Preview:
    out = operations.dispatch(
        "finance.sink.preview", {"sink": "rates"}, Role.OWNER, ctx=ledger.ctx
    )
    assert isinstance(out, Preview)
    return out


def requested(stub: StubServer) -> list[tuple[str, dict[str, str]]]:
    """Each request as (path, query)."""
    found = []
    for each in stub.requests:
        parts = urlsplit(each.path)
        found.append((parts.path, dict(parse_qsl(parts.query))))
    return found


def approve_current(ledger: Ledger, approve: Approve) -> None:
    approve("rates", spec_digest(valet.spec(ledger.uow)), at=T0)


@pytest.fixture
def usd_ledger(fx_ledger: Ledger, valet_stub: StubServer) -> Ledger:
    fx_ledger.file("synthetic-fx/usd.txt", fx_lines("USD", USD_LINE))
    fx_ledger.derive()
    return fx_ledger


# --- the gate -------------------------------------------------------------------------


def test_with_the_sink_off_the_fetch_sends_nothing(
    usd_ledger: Ledger,
    valet_stub: StubServer,
    sink_settings: Callable[..., None],
    record_sink_approval: Approve,
) -> None:
    out = fetch(usd_ledger)
    assert (out.enabled, out.planned, out.requests) == (False, 1, 0)
    # The setting set true by hand with no approval: still off.
    sink_settings(rates=True)
    assert fetch(usd_ledger).requests == 0
    # An approval with the setting false: still off.
    sink_settings(rates=False)
    approve_current(usd_ledger, record_sink_approval)
    assert fetch(usd_ledger).enabled is False
    assert valet_stub.requests == []
    assert usd_ledger.conn.scalar(select(rate.c.rate_date).limit(1)) is None


def test_with_an_approval_it_sends_one_request_per_series_and_stores_the_rates(
    fx_ledger: Ledger,
    valet_stub: StubServer,
    sink_settings: Callable[..., None],
    record_sink_approval: Approve,
) -> None:
    fx_ledger.file("synthetic-fx/usd.txt", fx_lines("USD", USD_LINE))
    fx_ledger.file("synthetic-fx/eur.txt", fx_lines("EUR", EUR_LINE))
    fx_ledger.derive()
    sink_settings(rates=True)
    approve_current(fx_ledger, record_sink_approval)
    valet_stub.reply(
        "/valet/observations/FXUSDCAD/json",
        body=valet_reply(
            "FXUSDCAD",
            [("2025-12-10", "1.3725"), ("2025-12-11", "1.3800"), ("2025-12-12", None)],
        ),
    )
    valet_stub.reply(
        "/valet/observations/FXEURCAD/json",
        body=valet_reply("FXEURCAD", [("2025-12-11", "1.6000")]),
    )

    out = fetch(fx_ledger)

    assert (out.enabled, out.requests, out.stored, out.failed) == (True, 2, 3, 0)
    # From RATE_GAP_DAYS before each currency's earliest line to the fetch day (the
    # fake clock's local date).
    assert sorted(requested(valet_stub)) == [
        (
            "/valet/observations/FXEURCAD/json",
            {"start_date": "2025-12-06", "end_date": "2026-01-15"},
        ),
        (
            "/valet/observations/FXUSDCAD/json",
            {"start_date": "2025-12-05", "end_date": "2026-01-15"},
        ),
    ]
    rows = fx_ledger.conn.execute(select(rate).order_by(rate.c.base, rate.c.rate_date))
    assert [(r.base, r.quote, r.rate_date, r.rate, r.source) for r in rows] == [
        ("EUR", "CAD", dt.date(2025, 12, 11), Decimal("1.6000"), "boc"),
        ("USD", "CAD", dt.date(2025, 12, 10), Decimal("1.3725"), "boc"),
        ("USD", "CAD", dt.date(2025, 12, 11), Decimal("1.3800"), "boc"),
    ]
    assert set(fx_ledger.conn.scalars(select(rate.c.fetched_at))) == {
        fx_ledger.ctx.clock.now_utc()
    }

    # The next derive converts with them; a second fetch needs nothing.
    fx_ledger.derive()
    states = fx_ledger.conn.execute(select(txn.c.currency, txn.c.conversion_state))
    assert sorted(tuple(row) for row in states) == [
        ("EUR", "converted"),
        ("USD", "converted"),
    ]
    assert fetch(fx_ledger).requests == 0
    assert len(valet_stub.requests) == 2


def test_an_eur_line_widens_the_spec_and_stales_a_usd_only_approval(
    usd_ledger: Ledger,
    valet_stub: StubServer,
    sink_settings: Callable[..., None],
    record_sink_approval: Approve,
) -> None:
    sink_settings(rates=True)
    approve_current(usd_ledger, record_sink_approval)
    assert is_enabled(usd_ledger.uow, "rates")
    before = preview(usd_ledger)
    assert [item.key for item in before.items] == ["FXUSDCAD"]

    usd_ledger.file("synthetic-fx/eur.txt", fx_lines("EUR", EUR_LINE))
    usd_ledger.derive()

    after = preview(usd_ledger)
    assert [item.key for item in after.items] == ["FXEURCAD", "FXUSDCAD"]
    assert after.spec_digest != before.spec_digest
    assert "series FXEURCAD: currency EUR; start_date 2025-12-06" in after.text
    assert "end_date 2026-01-15 (the fetch day)" in after.text
    assert is_enabled(usd_ledger.uow, "rates") is False
    out = fetch(usd_ledger)
    assert (out.enabled, out.requests) == (False, 0)
    assert valet_stub.requests == []


# --- the plan -------------------------------------------------------------------------


def test_a_2016_line_asks_the_legacy_noon_series_up_to_the_day_before_fx_begins(
    fx_ledger: Ledger,
    valet_stub: StubServer,
    sink_settings: Callable[..., None],
    record_sink_approval: Approve,
) -> None:
    fx_ledger.file(
        "synthetic-fx/usd.txt",
        fx_lines(
            "USD",
            "2016-12-28 -10.00 - - SAMPLE SHOP 99953",
            "2017-01-02 -10.00 - - SAMPLE SHOP 99954",
        ),
    )
    fx_ledger.derive()
    items = {item.key: dict(item.fields) for item in preview(fx_ledger).items}
    assert items == {
        valet.LEGACY_NOON_SERIES["USD"]: {
            "currency": "USD",
            "start_date": "2016-12-23",
            "end_date": "2017-01-02",
        }
    }
    sink_settings(rates=True)
    approve_current(fx_ledger, record_sink_approval)
    legacy = valet.LEGACY_NOON_SERIES["USD"]
    valet_stub.reply(
        f"/valet/observations/{legacy}/json",
        body=valet_reply(legacy, [("2016-12-28", "1.3500"), ("2016-12-30", "1.3400")]),
    )

    fetch(fx_ledger)

    assert requested(valet_stub) == [
        (
            f"/valet/observations/{legacy}/json",
            {"start_date": "2016-12-23", "end_date": "2017-01-02"},
        )
    ]
    fx_ledger.derive()
    states = fx_ledger.conn.execute(
        select(txn.c.description_norm, txn.c.conversion_state)
    )
    # The second line needs a later observation to prove the holiday: the FX series
    # has none stored yet, so it waits.
    assert dict(tuple(row) for row in states) == {
        "sample shop 99953": "converted",
        "sample shop 99954": "unconverted",
    }


def test_from_and_to_bound_every_series_of_the_set(
    usd_ledger: Ledger,
    valet_stub: StubServer,
    sink_settings: Callable[..., None],
    record_sink_approval: Approve,
) -> None:
    sink_settings(rates=True)
    approve_current(usd_ledger, record_sink_approval)

    fetch(usd_ledger, from_date=dt.date(2016, 6, 1), to_date=dt.date(2025, 12, 31))

    assert sorted(requested(valet_stub)) == [
        (
            "/valet/observations/FXUSDCAD/json",
            {"start_date": "2017-01-03", "end_date": "2025-12-31"},
        ),
        (
            f"/valet/observations/{valet.LEGACY_NOON_SERIES['USD']}/json",
            {"start_date": "2016-06-01", "end_date": "2017-01-02"},
        ),
    ]


# --- failures -------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "body", "reason"),
    [
        (404, b"not found", "http status 404"),
        (200, b"<html>maintenance</html>", "the reply was not understood"),
        (200, b'{"observations": [{"d": "2025-12-10", "FXUSDCAD": {"v": "-1"}}]}',
         "the reply was not understood"),
    ],
)  # fmt: skip
def test_a_failed_series_stores_nothing_and_does_not_stop_the_others(
    fx_ledger: Ledger,
    valet_stub: StubServer,
    sink_settings: Callable[..., None],
    record_sink_approval: Approve,
    status: int,
    body: bytes,
    reason: str,
) -> None:
    fx_ledger.file("synthetic-fx/usd.txt", fx_lines("USD", USD_LINE))
    fx_ledger.file("synthetic-fx/eur.txt", fx_lines("EUR", EUR_LINE))
    fx_ledger.derive()
    sink_settings(rates=True)
    approve_current(fx_ledger, record_sink_approval)
    valet_stub.reply("/valet/observations/FXUSDCAD/json", status=status, body=body)
    valet_stub.reply(
        "/valet/observations/FXEURCAD/json",
        body=valet_reply("FXEURCAD", [("2025-12-11", "1.6000")]),
    )

    out = fetch(fx_ledger)

    assert (out.requests, out.stored, out.failed) == (2, 1, 1)
    failed = next(each for each in out.series if each.outcome == "failed")
    assert (failed.series, failed.reason) == ("FXUSDCAD", reason)
    assert list(fx_ledger.conn.scalars(select(rate.c.base))) == ["EUR"]


# --- the command ----------------------------------------------------------------------


def test_finance_rate_fetch_says_the_sink_is_off_and_sends_nothing(
    usd_ledger: Ledger,
    valet_stub: StubServer,
    sink_settings: Callable[..., None],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(composition, "build_context", lambda _role: usd_ledger.ctx)

    code = main(["rate", "fetch"])

    err = capsys.readouterr().err
    assert code == 1
    assert "the rates sink is off; nothing was sent." in err
    assert "1 series would be requested" in err
    assert valet_stub.requests == []


def test_finance_rate_fetch_reports_counts_when_approved(
    usd_ledger: Ledger,
    valet_stub: StubServer,
    sink_settings: Callable[..., None],
    record_sink_approval: Approve,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    sink_settings(rates=True)
    approve_current(usd_ledger, record_sink_approval)
    valet_stub.reply(
        "/valet/observations/FXUSDCAD/json",
        body=valet_reply("FXUSDCAD", [("2025-12-10", "1.3725")]),
    )
    monkeypatch.setattr(composition, "build_context", lambda _role: usd_ledger.ctx)

    code = main(["rate", "fetch", "--to", "2025-12-31"])

    out = capsys.readouterr().out
    assert code == 0
    assert out.splitlines()[0] == (
        "finance rate fetch: 1 request(s) sent, 1 rate(s) stored, 0 failed."
    )
    assert "FXUSDCAD (USD) 2025-12-05 to 2025-12-31: stored 1 of 1 received." in out
    assert "1.3725" not in out
