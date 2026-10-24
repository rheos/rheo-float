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
from sqlalchemy import insert, select

from derive.support import Ledger, fx_lines
from rheo_finance import catalogue, composition, operations
from rheo_finance.cli.main import main
from rheo_finance.contracts import Role
from rheo_finance.rates import LEGACY_NOON_SERIES, valet
from rheo_finance.rates.valet import FetchOut
from rheo_finance.sinks.gate import is_enabled
from rheo_finance.sinks.ops import Preview
from rheo_finance.sinks.spec import spec_digest
from rheo_finance.storage.schema import rate, rate_coverage, txn

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


def coverage(ledger: Ledger) -> list[tuple[str, dt.date, dt.date]]:
    rows = ledger.conn.execute(
        select(
            rate_coverage.c.series,
            rate_coverage.c.covered_from,
            rate_coverage.c.covered_to,
        ).order_by(rate_coverage.c.series, rate_coverage.c.covered_from)
    )
    return [(row.series, row.covered_from, row.covered_to) for row in rows]


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
    # From LOOKBACK_DAYS before each currency's earliest line to the fetch day (the
    # fake clock's local date).
    assert sorted(requested(valet_stub)) == [
        (
            "/valet/observations/FXEURCAD/json",
            {"start_date": "2025-12-04", "end_date": "2026-01-15"},
        ),
        (
            "/valet/observations/FXUSDCAD/json",
            {"start_date": "2025-12-03", "end_date": "2026-01-15"},
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
    # Each series is covered from the requested start to the latest observation the
    # reply returned, not to the fetch day: the days after are not published yet.
    assert coverage(fx_ledger) == [
        ("FXEURCAD", dt.date(2025, 12, 4), dt.date(2025, 12, 11)),
        ("FXUSDCAD", dt.date(2025, 12, 3), dt.date(2025, 12, 11)),
    ]

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
    assert "series FXEURCAD: currency EUR; start_date 2025-12-04" in after.text
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
        LEGACY_NOON_SERIES["USD"]: {
            "currency": "USD",
            "start_date": "2016-12-21",
            "end_date": "2017-01-02",
        }
    }
    sink_settings(rates=True)
    approve_current(fx_ledger, record_sink_approval)
    legacy = LEGACY_NOON_SERIES["USD"]
    valet_stub.reply(
        f"/valet/observations/{legacy}/json",
        body=valet_reply(legacy, [("2016-12-28", "1.3500"), ("2016-12-30", "1.3400")]),
    )

    fetch(fx_ledger)

    assert requested(valet_stub) == [
        (
            f"/valet/observations/{legacy}/json",
            {"start_date": "2016-12-21", "end_date": "2017-01-02"},
        )
    ]
    fx_ledger.derive()
    states = fx_ledger.conn.execute(
        select(txn.c.description_norm, txn.c.conversion_state)
    )
    # The legacy series is closed, so its coverage runs to the requested end: the
    # holiday on its last day converts at the business day before, with no FX
    # coverage needed.
    assert dict(tuple(row) for row in states) == {
        "sample shop 99953": "converted",
        "sample shop 99954": "converted",
    }
    assert coverage(fx_ledger) == [
        (legacy, dt.date(2016, 12, 21), dt.date(2017, 1, 2)),
    ]


def test_an_empty_legacy_reply_records_no_coverage_and_is_asked_again(
    fx_ledger: Ledger,
    valet_stub: StubServer,
    sink_settings: Callable[..., None],
    record_sink_approval: Approve,
) -> None:
    legacy = LEGACY_NOON_SERIES["USD"]
    fx_ledger.file(
        "synthetic-fx/usd.txt",
        fx_lines(
            "USD",
            "2016-12-28 -10.00 - - SAMPLE SHOP 99953",
            "2017-01-02 -10.00 - - SAMPLE SHOP 99954",
        ),
    )
    fx_ledger.derive()
    sink_settings(rates=True)
    approve_current(fx_ledger, record_sink_approval)
    valet_stub.reply(f"/valet/observations/{legacy}/json", body=valet_reply(legacy, []))

    out = fetch(fx_ledger)

    assert (out.requests, out.stored, out.failed) == (1, 0, 0)
    # Closed or not, a reply with no observation covers nothing: a later line must
    # never convert at an older rate across days the fetch did not see.
    assert coverage(fx_ledger) == []
    fx_ledger.derive()
    assert set(fx_ledger.conn.scalars(select(txn.c.conversion_state))) == {
        "unconverted"
    }
    assert fetch(fx_ledger).requests == 1
    assert len(valet_stub.requests) == 2


def test_a_short_legacy_reply_covers_only_the_days_it_showed(
    fx_ledger: Ledger,
    valet_stub: StubServer,
    sink_settings: Callable[..., None],
    record_sink_approval: Approve,
) -> None:
    # Two legacy lines half a year apart; the reply holds one observation on the
    # first line's day and nothing after it.
    legacy = LEGACY_NOON_SERIES["USD"]
    fx_ledger.file(
        "synthetic-fx/usd.txt",
        fx_lines(
            "USD",
            "2016-06-15 -10.00 - - SAMPLE SHOP 99959",
            "2016-12-28 -10.00 - - SAMPLE SHOP 99960",
        ),
    )
    fx_ledger.derive()
    sink_settings(rates=True)
    approve_current(fx_ledger, record_sink_approval)
    valet_stub.reply(
        f"/valet/observations/{legacy}/json",
        body=valet_reply(legacy, [("2016-06-15", "1.2900")]),
    )

    fetch(fx_ledger)

    assert requested(valet_stub) == [
        (
            f"/valet/observations/{legacy}/json",
            {"start_date": "2016-06-08", "end_date": "2017-01-02"},
        )
    ]
    # The series is closed, but its latest observation is half a year before the
    # requested end: coverage stops at the observation, not at the end.
    assert coverage(fx_ledger) == [
        (legacy, dt.date(2016, 6, 8), dt.date(2016, 6, 15)),
    ]
    fx_ledger.derive()
    states = fx_ledger.conn.execute(
        select(txn.c.description_norm, txn.c.conversion_state)
    )
    assert dict(tuple(row) for row in states) == {
        "sample shop 99959": "converted",
        "sample shop 99960": "unconverted",
    }
    # The December line is planned again, from LOOKBACK_DAYS before it.
    items = {item.key: dict(item.fields) for item in preview(fx_ledger).items}
    assert items == {
        legacy: {
            "currency": "USD",
            "start_date": "2016-12-21",
            "end_date": "2017-01-02",
        }
    }


def test_an_open_series_covers_only_to_its_latest_observation(
    fx_ledger: Ledger,
    valet_stub: StubServer,
    sink_settings: Callable[..., None],
    record_sink_approval: Approve,
) -> None:
    # A line on the fetch day; the open FX series' reply ends the day before. That
    # day is within LOOKBACK_DAYS of the requested end, but only a closed series
    # extends across such days: the fetch day's rate is simply not published yet.
    fx_ledger.file(
        "synthetic-fx/usd.txt",
        fx_lines("USD", "2026-01-15 -10.00 - - SAMPLE SHOP 99961"),
    )
    fx_ledger.derive()
    sink_settings(rates=True)
    approve_current(fx_ledger, record_sink_approval)
    valet_stub.reply(
        "/valet/observations/FXUSDCAD/json",
        body=valet_reply("FXUSDCAD", [("2026-01-14", "1.3900")]),
    )

    fetch(fx_ledger)

    assert requested(valet_stub) == [
        (
            "/valet/observations/FXUSDCAD/json",
            {"start_date": "2026-01-08", "end_date": "2026-01-15"},
        )
    ]
    assert coverage(fx_ledger) == [
        ("FXUSDCAD", dt.date(2026, 1, 8), dt.date(2026, 1, 14)),
    ]
    fx_ledger.derive()
    assert fx_ledger.conn.scalar(select(txn.c.conversion_state)) == "unconverted"


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
            f"/valet/observations/{LEGACY_NOON_SERIES['USD']}/json",
            {"start_date": "2016-06-01", "end_date": "2017-01-02"},
        ),
    ]


def test_a_late_line_in_a_gap_between_fetches_is_planned_then_converted(
    fx_ledger: Ledger,
    valet_stub: StubServer,
    sink_settings: Callable[..., None],
    record_sink_approval: Approve,
) -> None:
    # Two earlier fetches left a Monday and the Monday two weeks on, with the days
    # between never fetched; then a Wednesday line arrives.
    fx_ledger.conn.execute(
        insert(rate),
        [
            {"rate_date": day, "base": "USD", "quote": "CAD", "rate": Decimal(v),
             "source": "boc", "fetched_at": T0}
            for day, v in ((dt.date(2025, 12, 1), "1.3000"),
                           (dt.date(2025, 12, 15), "1.3200"))
        ],
    )  # fmt: skip
    fx_ledger.conn.execute(
        insert(rate_coverage),
        [
            {"series": "FXUSDCAD", "source": "boc", "covered_from": first,
             "covered_to": last, "fetched_at": T0}
            for first, last in ((dt.date(2025, 11, 24), dt.date(2025, 12, 1)),
                                (dt.date(2025, 12, 15), dt.date(2025, 12, 15)))
        ],
    )  # fmt: skip
    fx_ledger.file(
        "synthetic-fx/usd.txt",
        fx_lines("USD", "2025-12-03 -10.00 - - SAMPLE SHOP 99957"),
    )
    fx_ledger.derive()
    assert fx_ledger.conn.scalar(select(txn.c.conversion_state)) == "unconverted"
    items = {item.key: dict(item.fields) for item in preview(fx_ledger).items}
    assert items == {"FXUSDCAD": {"currency": "USD", "start_date": "2025-11-26"}}

    sink_settings(rates=True)
    approve_current(fx_ledger, record_sink_approval)
    valet_stub.reply(
        "/valet/observations/FXUSDCAD/json",
        body=valet_reply(
            "FXUSDCAD", [("2025-12-02", "1.3100"), ("2025-12-03", "1.3150")]
        ),
    )
    fetch(fx_ledger)
    fx_ledger.derive()

    row = fx_ledger.conn.execute(
        select(txn.c.rate, txn.c.rate_date, txn.c.conversion_state)
    ).one()
    assert tuple(row) == (Decimal("1.3150"), dt.date(2025, 12, 3), "converted")


def test_a_line_with_an_issuer_stated_cad_amount_needs_no_rate(
    fx_ledger: Ledger,
    valet_stub: StubServer,
    sink_settings: Callable[..., None],
    record_sink_approval: Approve,
) -> None:
    fx_ledger.file(
        "synthetic-fx/usd.txt",
        fx_lines("USD", "2025-12-10 -10.00 -13.70 - SAMPLE STATED SHOP 99958"),
    )
    fx_ledger.derive()
    sink_settings(rates=True)
    approve_current(fx_ledger, record_sink_approval)

    assert preview(fx_ledger).items == []
    out = fetch(fx_ledger)

    assert (out.planned, out.requests) == (0, 0)
    assert valet_stub.requests == []


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


def test_a_redirect_to_another_host_fails_that_series_alone(
    fx_ledger: Ledger,
    valet_stub: StubServer,
    make_stub_server: Callable[[], StubServer],
    sink_settings: Callable[..., None],
    record_sink_approval: Approve,
) -> None:
    other = make_stub_server()
    fx_ledger.file("synthetic-fx/usd.txt", fx_lines("USD", USD_LINE))
    fx_ledger.file("synthetic-fx/eur.txt", fx_lines("EUR", EUR_LINE))
    fx_ledger.derive()
    sink_settings(rates=True)
    approve_current(fx_ledger, record_sink_approval)
    valet_stub.redirect(
        "/valet/observations/FXUSDCAD/json",
        other.url("/valet/observations/FXUSDCAD/json"),
    )
    valet_stub.reply(
        "/valet/observations/FXEURCAD/json",
        body=valet_reply("FXEURCAD", [("2025-12-11", "1.6000")]),
    )

    out = fetch(fx_ledger)

    assert (out.requests, out.stored, out.failed) == (2, 1, 1)
    failed = next(each for each in out.series if each.outcome == "failed")
    assert (failed.series, failed.reason) == ("FXUSDCAD", "the host was refused")
    assert list(fx_ledger.conn.scalars(select(rate.c.base))) == ["EUR"]
    assert other.requests == []


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
    assert "FXUSDCAD (USD) 2025-12-03 to 2025-12-31: stored 1 of 1 received." in out
    assert "1.3725" not in out
