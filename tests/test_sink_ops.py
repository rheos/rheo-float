"""The sink operations, ``finance sink ...`` and ``cli.approve`` (FR-65, AC-78 c, f).

A test-only ``mot`` sink: its provider declares the local stub's host, and its preview
builder returns one synthetic ticket whose amount a test can change. Its note shows the
ticket's wake moment against the clock, which the digest must ignore. ``cli.approve``
runs with fake terminals (``isatty`` and ``read_line`` injected). Every write stays in
the test's rolled-back transaction, except the reader-DSN test, which commits to a
throwaway database (``scratch_database``). Values are synthetic.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import os
import uuid
from collections.abc import Callable
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import Connection, create_engine, insert, select, text
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.pool import NullPool

from conftest import DatabaseUnderTest
from rheo_finance import catalogue, composition, operations
from rheo_finance.cli import approve
from rheo_finance.cli.main import main
from rheo_finance.clock import FakeClock, local_to_utc, render_local
from rheo_finance.context import FinanceContext, FinancePaths, UnitOfWork
from rheo_finance.contracts import Role
from rheo_finance.decision import ReverseIn
from rheo_finance.decision import reverse as reverse_decision
from rheo_finance.http import Client, SinkDisabled
from rheo_finance.operations import RoleRefused, dispatch
from rheo_finance.settings import SINKS, default_settings
from rheo_finance.sinks import gate
from rheo_finance.sinks.ops import CHANGED, Preview, Recorded, SinkRefused, StateOut
from rheo_finance.sinks.preview import (
    PREVIEW_BUILDERS,
    PreviewItem,
    SinkPreview,
    preview_digest,
)
from rheo_finance.sinks.spec import SINK_SPECS, RequestTemplate, SinkSpec, spec_digest
from rheo_finance.sinks.store import write_sink_specs
from rheo_finance.storage import schema
from rheo_finance.storage.ids import uuid7

if TYPE_CHECKING:
    from conftest import StubServer

SINK = "mot"
T0 = dt.datetime(2026, 1, 15, 12, 0, tzinfo=dt.UTC)  # make_context's fake clock
DUE = dt.date(2026, 1, 25)
WAKE_DAYS = 5
TICKETS = ("POST", "/api/tickets")
MakeContext = Callable[[Role], FinanceContext]

catalogue.load_all()


class TicketSink:
    """The ``mot`` provider (or ``name``'s): the stub's host and two ticket requests."""

    __test__ = False

    def __init__(self, host: str, name: str = SINK) -> None:
        self.hosts: tuple[str, ...] = (host,)
        self.name = name

    def __call__(self, _uow: UnitOfWork) -> SinkSpec:
        return SinkSpec(
            name=self.name,
            hosts=self.hosts,
            requests=(
                RequestTemplate(method="POST", path="/api/tickets"),
                RequestTemplate(method="PATCH", path="/api/tickets/:id"),
            ),
            key_file="mot.key",
            payload_policy_version=1,
        )


class TicketPreview:
    """One pending ticket; ``amount`` and ``pending`` change what is pending."""

    __test__ = False

    def __init__(self) -> None:
        self.amount = Decimal("120.00")
        self.pending = True

    def items(self, _uow: UnitOfWork) -> list[PreviewItem]:
        if not self.pending:
            return []
        return [
            PreviewItem(
                kind="ticket",
                key="bill:999-water:2026-01",
                amount=self.amount,
                currency="CAD",
                due_on=DUE,
                fields=(("status", "open"), ("title", "Water bill")),
            )
        ]

    def note(self, ctx: FinanceContext, item: PreviewItem) -> str | None:
        """The wake moment: rendered against now, so it lives in the text only."""
        assert item.due_on is not None
        tz = ctx.settings.timezone
        wake = local_to_utc(
            item.due_on - dt.timedelta(days=WAKE_DAYS), dt.time(8, 0), tz
        )
        if ctx.clock.now_utc() >= wake:
            return "wake passed"
        return f"wakes {render_local(wake, tz)}"


@pytest.fixture
def provider(stub_server: StubServer, monkeypatch: pytest.MonkeyPatch) -> TicketSink:
    sink = TicketSink(stub_server.host)
    monkeypatch.setitem(SINK_SPECS, SINK, sink)
    return sink


@pytest.fixture
def builder(monkeypatch: pytest.MonkeyPatch) -> TicketPreview:
    preview = TicketPreview()
    monkeypatch.setitem(
        PREVIEW_BUILDERS, SINK, SinkPreview(items=preview.items, note=preview.note)
    )
    return preview


@pytest.fixture
def owner(
    rolled_back_context: MakeContext, sink_settings: Callable[..., None]
) -> FinanceContext:
    # sink_settings: $FINANCE_DATA_DIR is a temporary directory, every sink false.
    return rolled_back_context(Role.OWNER)


@pytest.fixture
def cli_owner(owner: FinanceContext, monkeypatch: pytest.MonkeyPatch) -> FinanceContext:
    monkeypatch.setattr(composition, "build_context", lambda role: owner)
    return owner


def fake_clock(ctx: FinanceContext) -> FakeClock:
    assert isinstance(ctx.clock, FakeClock)
    return ctx.clock


def get_preview(ctx: FinanceContext) -> Preview:
    out = dispatch("finance.sink.preview", {"sink": SINK}, Role.OWNER, ctx=ctx)
    assert isinstance(out, Preview)
    return out


def approval(preview: Preview, sink: str = SINK) -> dict[str, str | None]:
    """The ``finance.sink.approve`` input ``cli.approve`` sends for ``preview``."""
    return {
        "sink": sink,
        "digest_prefix": preview.digest[:8],
        "spec_digest": preview.spec_digest,
    }


def sink_decisions(
    conn: Connection, kind: str = "sink_approve"
) -> list[dict[str, object]]:
    rows = conn.execute(
        select(schema.decision).where(schema.decision.c.kind == kind)
    ).mappings()
    return [dict(row) for row in rows]


def terminal(_fd: int) -> bool:
    return True


def typing(line: str, before: Callable[[], None] = lambda: None) -> Callable[[], str]:
    """A fake keyboard: runs ``before`` (as if time passed while typing), then types."""

    def read_line() -> str:
        before()
        return line + "\n"

    return read_line


# --- the clock-free preview digest ----------------------------------------------------


def test_a_preview_at_two_clock_times_has_one_digest_and_an_amount_change_another(
    owner: FinanceContext, provider: TicketSink, builder: TicketPreview
) -> None:
    early = get_preview(owner)
    fake_clock(owner).set(dt.datetime(2026, 1, 22, 18, 30, tzinfo=dt.UTC))
    late = get_preview(owner)

    # The text a person reads moved with the clock: the "as of" line and the wake.
    assert early.text != late.text
    assert "as of 2026-01-15" in early.text and "as of 2026-01-22" in late.text
    assert "wakes 2026-01-20 08:00" in early.text
    assert "wake passed" in late.text and "wakes" not in late.text
    assert "due in 10 day(s)" in early.text and "due in 3 day(s)" in late.text
    # The digest did not.
    assert early.digest == late.digest
    assert early.digest[:8] in early.text

    builder.amount = Decimal("125.00")
    changed = get_preview(owner)
    assert changed.digest != early.digest


def test_the_digest_is_sha256_of_the_canonical_items(
    owner: FinanceContext, provider: TicketSink, builder: TicketPreview
) -> None:
    out = get_preview(owner)
    assert out.digest == preview_digest(out.items)
    assert len(out.items) == 1
    assert out.requests == ["POST /api/tickets", "PATCH /api/tickets/:id"]
    assert out.key_file == "mot.key"
    assert out.setting_enabled is False


def test_with_nothing_pending_the_preview_shows_a_sample(
    owner: FinanceContext, provider: TicketSink, builder: TicketPreview
) -> None:
    builder.pending = False
    out = get_preview(owner)
    assert out.items == []
    assert out.digest == hashlib.sha256(b"[]").hexdigest()
    assert "nothing pending" in out.text
    assert "- sample sample:1: 99.00 CAD" in out.text


def test_the_preview_sends_nothing_even_with_the_sink_enabled(
    owner: FinanceContext,
    owner_conn: Connection,
    provider: TicketSink,
    builder: TicketPreview,
    sink_settings: Callable[..., None],
    record_sink_approval: Callable[..., uuid.UUID],
    stub_server: StubServer,
) -> None:
    sink_settings(mot=True)
    record_sink_approval(SINK, spec_digest(provider(UnitOfWork(owner_conn))), at=T0)
    assert gate.is_enabled(UnitOfWork(owner_conn), SINK)
    out = get_preview(owner)
    assert out.setting_enabled is True
    assert stub_server.requests == []


def test_a_sink_with_no_preview_builder_is_refused(owner: FinanceContext) -> None:
    with pytest.raises(SinkRefused, match="no preview"):
        dispatch("finance.sink.preview", {"sink": "imap"}, Role.OWNER, ctx=owner)


# --- finance sink approve, end to end with fake terminals ----------------------------


@pytest.mark.parametrize(
    "tty", [{0: False, 1: True}, {0: True, 1: False}, {0: False, 1: False}]
)
def test_approve_refuses_without_a_terminal_on_stdin_and_stdout(
    tty: dict[int, bool],
    cli_owner: FinanceContext,
    owner_conn: Connection,
    provider: TicketSink,
    builder: TicketPreview,
    capsys: pytest.CaptureFixture[str],
) -> None:
    typed = typing("approve mot 00000000")
    assert approve.run(SINK, isatty=tty.__getitem__, read_line=typed) == 1
    captured = capsys.readouterr()
    assert captured.out == ""  # no preview was even built
    assert captured.err.startswith(
        "finance sink approve mot: refused. It runs only in a terminal"
    )
    assert sink_decisions(owner_conn) == []


def test_the_command_refuses_under_a_plain_call_with_no_terminal(
    cli_owner: FinanceContext,
    owner_conn: Connection,
    provider: TicketSink,
    builder: TicketPreview,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # The real isatty. Under a plain pytest run stdout is captured, so this refuses;
    # under `pytest -s` in a terminal it would wait on the keyboard instead.
    if os.isatty(0) and os.isatty(1):
        pytest.skip(
            "stdin and stdout are terminals here; the fake-terminal tests cover it"
        )
    assert main(["sink", "approve", SINK]) == 1
    assert "runs only in a terminal" in capsys.readouterr().err
    assert sink_decisions(owner_conn) == []


def test_approve_refuses_a_wrong_typed_code(
    cli_owner: FinanceContext,
    owner_conn: Connection,
    provider: TicketSink,
    builder: TicketPreview,
    capsys: pytest.CaptureFixture[str],
) -> None:
    code = get_preview(cli_owner).digest[:8]
    wrong = "0" * 8 if code != "0" * 8 else "1" * 8
    for line in (f"approve mot {wrong}", f"approve rates {code}", "yes", ""):
        assert approve.run(SINK, isatty=terminal, read_line=typing(line)) == 1
        captured = capsys.readouterr()
        assert f"type exactly: approve mot {code}" in captured.out
        assert "The typed line did not match" in captured.err
    assert sink_decisions(owner_conn) == []


def test_approve_refuses_when_the_pending_items_changed(
    cli_owner: FinanceContext,
    owner_conn: Connection,
    provider: TicketSink,
    builder: TicketPreview,
    capsys: pytest.CaptureFixture[str],
) -> None:
    code = get_preview(cli_owner).digest[:8]

    def a_new_amount_arrives() -> None:
        builder.amount = Decimal("130.00")

    typed = typing(f"approve mot {code}", before=a_new_amount_arrives)
    assert approve.run(SINK, isatty=terminal, read_line=typed) == 1
    err = capsys.readouterr().err
    assert f"mot: {CHANGED}" in err
    assert sink_decisions(owner_conn) == []


def test_approve_refuses_when_the_spec_widened_after_the_preview(
    cli_owner: FinanceContext,
    owner_conn: Connection,
    provider: TicketSink,
    builder: TicketPreview,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The typed line is only the preview digest8; the spec digest the preview showed
    rides along, so a sink widened while the owner typed is refused, not approved."""
    code = get_preview(cli_owner).digest[:8]

    def a_build_widens_the_sink() -> None:
        provider.hosts += ("tickets.example.test",)

    typed = typing(f"approve mot {code}", before=a_build_widens_the_sink)
    assert approve.run(SINK, isatty=terminal, read_line=typed) == 1
    assert f"mot: {CHANGED}" in capsys.readouterr().err
    assert sink_decisions(owner_conn) == []


def test_the_handler_refuses_a_spec_digest_other_than_the_current_one(
    owner: FinanceContext, provider: TicketSink, builder: TicketPreview
) -> None:
    payload = approval(get_preview(owner)) | {"spec_digest": "9" * 64}
    with pytest.raises(SinkRefused, match=CHANGED):
        dispatch("finance.sink.approve", payload, Role.OWNER, ctx=owner)


def test_the_handler_refuses_a_missing_spec_digest_when_a_spec_exists(
    owner: FinanceContext, provider: TicketSink, builder: TicketPreview
) -> None:
    payload = approval(get_preview(owner)) | {"spec_digest": None}
    with pytest.raises(SinkRefused, match=CHANGED):
        dispatch("finance.sink.approve", payload, Role.OWNER, ctx=owner)


def test_approve_records_one_decision_and_the_sink_then_sends_once(
    cli_owner: FinanceContext,
    owner_conn: Connection,
    provider: TicketSink,
    builder: TicketPreview,
    sink_settings: Callable[..., None],
    stub_server: StubServer,
    capsys: pytest.CaptureFixture[str],
) -> None:
    uow = UnitOfWork(owner_conn)
    sink_settings(mot=True)  # set by hand before approving: still off
    ticket_client = Client(
        SINK, provider(uow), (TICKETS,), uow=uow, allowed_schemes=("http",)
    )
    with pytest.raises(SinkDisabled):
        ticket_client.request("POST", stub_server.url("/api/tickets"), body=b"{}")

    preview = get_preview(cli_owner)
    typed = typing(f"approve mot {preview.digest[:8]}")
    assert approve.run(SINK, isatty=terminal, read_line=typed) == 0
    out = capsys.readouterr().out
    assert "this preview sends nothing" in out
    assert "Setting finance.sinks.mot.enabled is true" in out
    assert f"approved against preview digest {preview.digest[:8]}" in out
    assert stub_server.requests == []  # approving sent nothing

    [row] = sink_decisions(owner_conn)
    assert row["sink"] == SINK
    assert row["spec_digest"] == spec_digest(provider(uow))
    assert row["preview_digest"] == preview.digest
    assert row["decided_at"] == T0
    assert f"Recorded as decision {row['id']}" in out

    ticket_client.request("POST", stub_server.url("/api/tickets"), body=b"{}")
    assert [(r.method, r.path) for r in stub_server.requests] == [TICKETS]


def test_approve_with_the_setting_off_says_so_and_does_not_set_it(
    cli_owner: FinanceContext,
    owner_conn: Connection,
    provider: TicketSink,
    builder: TicketPreview,
    capsys: pytest.CaptureFixture[str],
) -> None:
    code = get_preview(cli_owner).digest[:8]
    typed = typing(f"approve mot {code}")
    assert approve.run(SINK, isatty=terminal, read_line=typed) == 0
    out = capsys.readouterr().out
    assert "enabled is false: set it to true in settings.toml" in out
    assert gate.is_enabled(UnitOfWork(owner_conn), SINK) is False


def test_the_handler_recomputes_and_refuses_a_stale_prefix(
    owner: FinanceContext, provider: TicketSink, builder: TicketPreview
) -> None:
    payload = approval(get_preview(owner))
    builder.amount = Decimal("1.00")
    with pytest.raises(SinkRefused, match=CHANGED):
        dispatch("finance.sink.approve", payload, Role.OWNER, ctx=owner)


@pytest.mark.parametrize("role", [Role.SERVICE, Role.MEMBER])
def test_dispatch_refuses_approve_for_any_role_but_the_owner(
    role: Role,
    rolled_back_context: MakeContext,
    owner_conn: Connection,
    provider: TicketSink,
    builder: TicketPreview,
    sink_settings: Callable[..., None],
) -> None:
    payload = approval(get_preview(rolled_back_context(Role.OWNER)))
    with pytest.raises(RoleRefused, match="refuses role"):
        dispatch("finance.sink.approve", payload, role, ctx=rolled_back_context(role))
    assert sink_decisions(owner_conn) == []


def test_approve_refuses_a_sink_with_no_spec(
    owner: FinanceContext, builder: TicketPreview
) -> None:
    payload = approval(get_preview(owner))
    assert payload["spec_digest"] is None
    with pytest.raises(SinkRefused, match="nothing to approve"):
        dispatch("finance.sink.approve", payload, Role.OWNER, ctx=owner)


def test_approve_rewrites_sink_spec_so_report_sink_is_current_at_once(
    cli_owner: FinanceContext,
    owner_conn: Connection,
    provider: TicketSink,
    builder: TicketPreview,
) -> None:
    assert report_row(owner_conn) is None  # no derive has run in this test
    code = get_preview(cli_owner).digest[:8]
    assert (
        approve.run(SINK, isatty=terminal, read_line=typing(f"approve mot {code}")) == 0
    )
    row = report_row(owner_conn)
    assert row is not None
    assert row["approved_at"] == T0
    assert row["stale"] is False


# --- disable -------------------------------------------------------------------------


def test_disable_needs_no_terminal_and_turns_the_sink_off(
    cli_owner: FinanceContext,
    owner_conn: Connection,
    provider: TicketSink,
    builder: TicketPreview,
    sink_settings: Callable[..., None],
    capsys: pytest.CaptureFixture[str],
) -> None:
    sink_settings(mot=True)
    code = get_preview(cli_owner).digest[:8]
    assert (
        approve.run(SINK, isatty=terminal, read_line=typing(f"approve mot {code}")) == 0
    )
    assert gate.is_enabled(UnitOfWork(owner_conn), SINK)
    fake_clock(cli_owner).advance(dt.timedelta(minutes=1))
    capsys.readouterr()

    provider.hosts += ("tickets.example.test",)
    assert main(["sink", "disable", SINK]) == 0
    out = capsys.readouterr().out
    assert out.startswith("finance sink disable mot: disabled;")
    assert len(sink_decisions(owner_conn, "sink_disable")) == 1
    assert gate.is_enabled(UnitOfWork(owner_conn), SINK) is False
    # Disable rewrote sink_spec too: it holds the widened spec's digest.
    written = owner_conn.scalar(
        select(schema.sink_spec.c.spec_digest).where(schema.sink_spec.c.sink == SINK)
    )
    assert written == spec_digest(provider(UnitOfWork(owner_conn)))


@pytest.mark.parametrize("failure", ["python", "sql"])
def test_disable_lands_even_when_the_providers_spec_refresh_fails(
    failure: str,
    owner: FinanceContext,
    owner_conn: Connection,
    provider: TicketSink,
    builder: TicketPreview,
    sink_settings: Callable[..., None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """FR-65: the owner can disable at any time. A provider that raises, or one whose
    SQL fails and would abort the transaction, cannot roll the disable back."""
    sink_settings(mot=True)
    dispatch(
        "finance.sink.approve", approval(get_preview(owner)), Role.OWNER, ctx=owner
    )
    uow = UnitOfWork(owner_conn)
    assert gate.is_enabled(uow, SINK)
    fake_clock(owner).advance(dt.timedelta(minutes=1))

    def broken(inner: UnitOfWork) -> SinkSpec:
        if failure == "sql":
            inner.execute(text("SELECT 1 / 0"))
        raise RuntimeError("the provider failed")

    monkeypatch.setitem(SINK_SPECS, SINK, broken)
    out = dispatch("finance.sink.disable", {"sink": SINK}, Role.OWNER, ctx=owner)
    assert isinstance(out, Recorded)
    assert out.spec_refreshed is False
    assert len(sink_decisions(owner_conn, "sink_disable")) == 1
    assert gate.is_enabled(uow, SINK) is False  # the provider is not even asked


# --- report_sink and finance.sink.state ----------------------------------------------


def report_row(conn: Connection, sink: str = SINK) -> dict[str, object] | None:
    found = (
        conn.execute(
            select(schema.report_sink).where(schema.report_sink.c.sink == sink)
        )
        .mappings()
        .first()
    )
    return None if found is None else dict(found)


AGREE_SINK = "telegram"  # not mot: the reader test commits mot rows to the scratch DB


def test_report_sink_agrees_with_the_gate_case_by_case(
    scratch_database: DatabaseUnderTest,
    stub_server: StubServer,
    monkeypatch: pytest.MonkeyPatch,
    sink_settings: Callable[..., None],
    tmp_path: Path,
) -> None:
    """Approve, widen, approve again, disable, approve, reverse: after each step the
    view's "approved and current" equals the gate's verdict (the setting is true).

    On the scratch database, migrated fresh from the frozen revisions in this run (so a
    view changed in code but not in a revision is what is tested against), inside one
    transaction that is rolled back."""
    provider = TicketSink(stub_server.host, name=AGREE_SINK)
    monkeypatch.setitem(SINK_SPECS, AGREE_SINK, provider)
    sink_settings(telegram=True)
    owner = scratch_context(scratch_database, Role.OWNER, tmp_path)
    hour = dt.timedelta(hours=1)
    engine = create_engine(scratch_database.owner_url, poolclass=NullPool)
    try:
        with engine.connect() as conn:
            transaction = conn.begin()
            uow = UnitOfWork(conn)

            def decide(kind: str, at: dt.datetime, **columns: object) -> uuid.UUID:
                decision_id = uuid7(at)
                conn.execute(
                    insert(schema.decision).values(
                        id=decision_id,
                        kind=kind,
                        decided_at=at,
                        sink=AGREE_SINK,
                        **columns,
                    )
                )
                return decision_id

            def approve_at(at: dt.datetime) -> uuid.UUID:
                current = spec_digest(provider(uow))
                return decide(
                    "sink_approve", at, spec_digest=current, preview_digest="9" * 64
                )

            def check(approved: bool, stale: bool) -> None:
                write_sink_specs(uow)
                row = report_row(conn, AGREE_SINK)
                assert row is not None
                assert (row["approved_at"] is not None) is approved
                assert row["stale"] is stale
                assert gate.is_enabled(uow, AGREE_SINK) is (approved and not stale)

            try:
                check(approved=False, stale=False)  # a spec, no decision
                first = approve_at(T0)
                check(approved=True, stale=False)
                provider.hosts += ("tickets.example.test",)
                check(approved=True, stale=True)
                approve_at(T0 + hour)
                check(approved=True, stale=False)
                decide("sink_disable", T0 + 2 * hour)
                check(approved=False, stale=False)
                newest = approve_at(T0 + 3 * hour)
                check(approved=True, stale=False)
                # Reversing the newest approval does not fall back to the older ones.
                fake_clock(owner).set(T0 + 4 * hour)
                reverse_decision(owner, uow, ReverseIn(decision_id=newest))
                check(approved=False, stale=False)
                assert first != newest
            finally:
                transaction.rollback()
    finally:
        engine.dispose()


def test_state_runs_on_the_reader_and_lists_every_sink(
    rolled_back_context: MakeContext,
    owner_conn: Connection,
    provider: TicketSink,
    sink_settings: Callable[..., None],
    record_sink_approval: Callable[..., uuid.UUID],
) -> None:
    """``state`` opens the reader DSN, which cannot see this test's uncommitted
    approval: it reads only what is committed. The reader-DSN test below commits."""
    uow = UnitOfWork(owner_conn)
    sink_settings(mot=True)
    write_sink_specs(uow)
    record_sink_approval(SINK, spec_digest(provider(uow)), at=T0)
    assert gate.is_enabled(uow, SINK)  # true on the owner's own transaction
    for role in (Role.OWNER, Role.MEMBER, Role.SERVICE):
        out = dispatch("finance.sink.state", {}, role, ctx=rolled_back_context(role))
        assert isinstance(out, StateOut)
        assert [each.sink for each in out.sinks] == list(SINKS)
        by_sink = {each.sink: each for each in out.sinks}
        assert by_sink[SINK].setting_enabled is True
        assert by_sink[SINK].approval == "none"
        assert by_sink[SINK].enabled is False


def scratch_context(
    database: DatabaseUnderTest, role: Role, data: Path
) -> FinanceContext:
    ctx = FinanceContext(
        settings=default_settings(),
        clock=FakeClock(T0),
        paths=FinancePaths(data=data),
        role=role,
        snapshot_provider=lambda _uow, _ref: None,
    )
    return operations.with_databases(
        ctx, owner_url=database.owner_url, reader_url=database.reader_url
    )


def test_report_sink_under_the_reader_dsn_shows_stale_after_a_provider_change(
    scratch_database: DatabaseUnderTest,
    provider: TicketSink,
    builder: TicketPreview,
    sink_settings: Callable[..., None],
    tmp_path: Path,
) -> None:
    scratch = scratch_database
    owner_engine = create_engine(scratch.owner_url, poolclass=NullPool)
    reader_engine = create_engine(scratch.reader_url, poolclass=NullPool)
    owner = scratch_context(scratch, Role.OWNER, tmp_path)
    member = scratch_context(scratch, Role.MEMBER, tmp_path)
    sink_settings(mot=True)

    def derive() -> None:
        with owner_engine.begin() as conn:
            write_sink_specs(UnitOfWork(conn))

    def mot_state() -> tuple[str, bool]:
        out = dispatch("finance.sink.state", {}, Role.MEMBER, ctx=member)
        assert isinstance(out, StateOut)
        [mot] = [each for each in out.sinks if each.sink == SINK]
        return mot.approval, mot.enabled

    try:
        assert mot_state() == ("none", False)
        recorded = dispatch(
            "finance.sink.approve", approval(get_preview(owner)), Role.OWNER, ctx=owner
        )
        assert isinstance(recorded, Recorded)
        # No derive ran: the approval rewrote sink_spec, so the reader sees it current.
        assert mot_state() == ("current", True)

        provider.hosts += ("tickets.example.test",)  # a later build widens the sink
        derive()
        assert mot_state() == ("stale", False)
        with reader_engine.connect() as conn:
            stale = conn.execute(
                text("SELECT stale FROM finance.report_sink WHERE sink = 'mot'")
            ).scalar_one()
            assert stale is True
            # The reader reads the views and nothing else.
            for table in ("decision", "sink_spec"):
                with pytest.raises(ProgrammingError), conn.begin_nested():
                    conn.execute(text(f"SELECT count(*) FROM finance.{table}"))
        with owner_engine.connect() as conn:
            assert gate.is_enabled(UnitOfWork(conn), SINK) is False
    finally:
        owner_engine.dispose()
        reader_engine.dispose()


# --- the other commands --------------------------------------------------------------


def test_preview_summary_prints_counts_requests_digest8_and_the_setting(
    cli_owner: FinanceContext,
    provider: TicketSink,
    builder: TicketPreview,
    capsys: pytest.CaptureFixture[str],
) -> None:
    digest = get_preview(cli_owner).digest
    assert main(["sink", "preview", SINK, "--summary"]) == 0
    out = capsys.readouterr().out
    assert out.splitlines() == [
        f"finance sink preview mot: 1 item(s) pending, digest {digest[:8]};"
        " this preview sends nothing.",
        "Requests: POST /api/tickets, PATCH /api/tickets/:id.",
        "Setting finance.sinks.mot.enabled is false.",
    ]
    assert "120.00" not in out and "bill:" not in out


def test_preview_full_prints_the_items(
    cli_owner: FinanceContext,
    provider: TicketSink,
    builder: TicketPreview,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["sink", "preview", SINK]) == 0
    out = capsys.readouterr().out
    assert out.startswith("finance sink preview mot: 1 item(s) pending;")
    assert "- ticket bill:999-water:2026-01: 120.00 CAD; due 2026-01-25" in out
    assert "- POST /api/tickets" in out
    assert chr(0x2014) not in out


def test_status_names_each_sink(
    cli_owner: FinanceContext, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["sink", "status"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[0] == "finance sink status: 0 of 9 sinks enabled."
    assert lines[1] == "Stale approvals: none."
    assert "mot: off; setting false; no approval." in lines


def test_an_unknown_sink_is_a_usage_error(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["sink", "approve", "nowhere"]) == 2
    assert main(["sink", "preview", "nowhere"]) == 2
    assert "invalid choice" in capsys.readouterr().err
