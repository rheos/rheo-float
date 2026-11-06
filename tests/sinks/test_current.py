"""Reconcile public desired state to local Current operations, never a live sink."""

from __future__ import annotations

import datetime as dt
import json
from collections.abc import Callable
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import Connection, Engine, delete, func, select, update

from derive.support import Ledger
from reminders.test_desired import add
from rheo_finance import catalogue
from rheo_finance.clock import FakeClock
from rheo_finance.context import FinanceContext, UnitOfWork
from rheo_finance.contracts import Role
from rheo_finance.derive.fingerprint import fingerprint
from rheo_finance.derive.ops import DeriveOut, DigestOut, RebuildOut
from rheo_finance.operations import OperationError, dispatch
from rheo_finance.reminders.desired import DesiredOut, desired_tickets
from rheo_finance.reminders.store import PostgresReminderRepository
from rheo_finance.settings import parse_settings
from rheo_finance.sinks import current
from rheo_finance.sinks.current_store import PostgresCurrentTicketRepository, SinkTicket
from rheo_finance.sinks.gate import is_enabled
from rheo_finance.sinks.ops import CHANGED, Preview, Recorded, SinkRefused
from rheo_finance.sinks.spec import spec_digest
from rheo_finance.storage.schema import decision, obligation, payee, sink_ticket
from sinks.support import CurrentStub, WireFields


@pytest.mark.parametrize("credential_state", ["missing", "malformed"])
@pytest.mark.parametrize(
    "operation", ["finance.ledger.derive", "finance.ledger.rebuild"]
)
def test_public_ledger_stays_offline_with_unavailable_current_credentials(
    own: Ledger,
    current_context: FinanceContext,
    current_stub: CurrentStub,
    sink_settings: Callable[..., None],
    monkeypatch: pytest.MonkeyPatch,
    credential_state: str,
    operation: str,
) -> None:
    from derive.support import read_as_text
    from derive.test_payment_application import statement_n, with_settings
    from rheo_finance.parsers import docextract_adapter

    monkeypatch.setattr(docextract_adapter, "pdf_text", read_as_text)
    own.ctx = replace(own.ctx, settings=current_context.settings)
    with_settings(own, '[finance.reminders]\nmin_due_date = "2025-01-01"\n')
    statement_n(own)
    own.derive()
    catalogue.load_all()
    assert current.sync_reminders(own.ctx).state == "synced"
    delivery = rows(own.conn)
    ledger_rows = own.txns()
    obligations = tuple(own.conn.execute(select(obligation)).mappings())
    available_digest = spec_digest(current.spec(own.uow))
    current_stub.requests.clear()

    # The existing fixture owns settings writes; only synthetic env files change.
    sink_settings(current=False)
    disabled = replace(
        current_context.settings,
        values={
            **current_context.settings.values,
            "finance.sinks.current.enabled": False,
        },
    )
    assert disabled.get("finance.reminders.current_base_url") is None
    monkeypatch.setattr(current, "load_settings", lambda: disabled)
    credential = Path(str(disabled.get("finance.reminders.current_credential_file")))
    if credential_state == "missing":
        credential.unlink()
    else:
        credential.write_text("export CURRENT_API_URL='unterminated\n")
    assert not is_enabled(own.uow, "current")

    result = dispatch(operation, {}, own.ctx.role, ctx=own.ctx)
    assert isinstance(result, DeriveOut)
    assert own.txns() == ledger_rows
    assert tuple(own.conn.execute(select(obligation)).mappings()) == obligations
    assert rows(own.conn) == delivery
    unavailable = current.spec(own.uow)
    assert unavailable.hosts == ()
    assert spec_digest(unavailable) != available_digest
    assert current.sync_reminders(own.ctx) == current.SyncResult(
        "pending", "configuration_error"
    )
    assert not is_enabled(own.uow, "current")
    assert rows(own.conn) == delivery and current_stub.requests == []


@pytest.mark.parametrize(
    "operation,field",
    [("update", "due_on"), ("update", "priority"), ("set_status", "due_on")],
)
def test_cas_field_race_preserves_ack_until_all_desired_fields_converge(
    current_context: FinanceContext,
    current_stub: CurrentStub,
    owner_conn: Connection,
    operation: str,
    field: str,
) -> None:
    add(owner_conn)
    current.sync_reminders(current_context)
    before = rows(owner_conn)[0]
    owner_conn.execute(
        update(obligation).values(
            threshold_remaining=Decimal("0.00")
            if operation == "set_status"
            else Decimal("15.00"),
            status="paid" if operation == "set_status" else "partial",
        )
    )

    def edit_matching_field(op: str, inputs: WireFields) -> None:
        if op != operation:
            return
        ref = inputs["ref"]
        assert isinstance(ref, str)
        item = current_stub.items[ref]
        if field == "due_on":
            item["due_on"] = "2026-05-22"
        else:
            item["priority"] = "low"
        item["revision"] += 1
        current_stub.before_mutation = None

    current_stub.before_mutation = edit_matching_field
    current_stub.requests.clear()
    assert current.sync_reminders(current_context) == current.SyncResult(
        "failed", "incomplete_convergence"
    )
    after = rows(owner_conn)[0]
    assert (
        after.last_title,
        after.last_due_on,
        after.last_priority,
        after.last_status,
    ) == (
        before.last_title,
        before.last_due_on,
        before.last_priority,
        before.last_status,
    )
    assert after.external_id == before.external_id and after.state == before.state
    assert after.last_error_code == "incomplete_convergence"
    assert current_stub.operations() == (
        ["get", "update", "get", "update"]
        if operation == "update"
        else ["get", "update", "get", "set_status", "get", "set_status"]
    )
    current_stub.requests.clear()
    assert current.sync_reminders(current_context).state == "synced"
    assert current_stub.operations(writes=True) == ["update"]
    assert len(current_stub.items) == 1
    assert rows(owner_conn)[0].last_error_code is None


def test_public_rebuild_preserves_current_delivery_state_without_dirtying_ledger(
    own: Ledger,
    current_context: FinanceContext,
    current_stub: CurrentStub,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from derive.support import read_as_text
    from derive.test_payment_application import statement_n, with_settings
    from rheo_finance.parsers import docextract_adapter

    monkeypatch.setattr(docextract_adapter, "pdf_text", read_as_text)
    own.ctx = replace(own.ctx, settings=current_context.settings)
    with_settings(own, '[finance.reminders]\nmin_due_date = "2025-01-01"\n')
    statement_n(own)
    own.derive()
    catalogue.load_all()
    before = dispatch("finance.ledger.derive", {}, own.ctx.role, ctx=own.ctx)
    content = dispatch("finance.ledger.digest", {}, own.ctx.role, ctx=own.ctx)
    assert isinstance(before, DeriveOut) and isinstance(content, DigestOut)

    assert current.sync_reminders(own.ctx).state == "synced"
    assert current_stub.operations(writes=True) == ["create"]
    (ticket,) = rows(own.conn)
    store = PostgresCurrentTicketRepository(own.uow)
    updated = ticket.model_copy(
        update={
            "last_error_code": "network_error",
            "last_attempt_at": own.ctx.clock.now_utc() + dt.timedelta(seconds=1),
        }
    )
    store.save(updated)
    assert fingerprint(own.uow, own.ctx).digest == before.fingerprint
    after_state = dispatch("finance.ledger.digest", {}, own.ctx.role, ctx=own.ctx)
    assert isinstance(after_state, DigestOut) and after_state == content
    current_stub.requests.clear()

    rebuilt = dispatch("finance.ledger.rebuild", {}, own.ctx.role, ctx=own.ctx)
    assert isinstance(rebuilt, RebuildOut) and rebuilt.fingerprint == before.fingerprint
    assert rows(own.conn) == (updated,)
    after_rebuild = dispatch("finance.ledger.digest", {}, own.ctx.role, ctx=own.ctx)
    assert isinstance(after_rebuild, DigestOut) and after_rebuild == content
    assert fingerprint(own.uow, own.ctx).digest == before.fingerprint
    assert current_stub.requests == []


def public_preview(ctx: FinanceContext) -> Preview:
    catalogue.load_all()
    result = dispatch("finance.sink.preview", {"sink": "current"}, ctx.role, ctx=ctx)
    assert isinstance(result, Preview)
    assert f"Preview digest {result.digest[:8]} (full {result.digest})." in result.text
    return result


def approval_count(conn: Connection) -> int:
    return int(
        conn.scalar(
            select(func.count())
            .select_from(decision)
            .where(decision.c.kind == "sink_approve", decision.c.sink == "current")
        )
        or 0
    )


def test_public_preview_crosses_midnight_with_fixed_state_and_refuses_old_code(
    current_context: FinanceContext,
    current_stub: CurrentStub,
    owner_conn: Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    add(owner_conn)
    clock = FakeClock(
        dt.datetime(2026, 5, 20, 23, 59, tzinfo=current_context.settings.timezone)
    )
    ctx = replace(current_context, role=Role.OWNER, clock=clock)
    monkeypatch.setattr(current, "SystemClock", lambda: clock)
    assert current.sync_reminders(ctx).state == "synced"
    current_stub.requests.clear()
    acknowledged = rows(owner_conn)
    assert acknowledged[0].external_id is not None
    assert acknowledged[0].last_priority == "normal"
    database = tuple(owner_conn.execute(select(obligation)).mappings())
    approvals = approval_count(owner_conn)

    early = public_preview(ctx)
    assert early.items == []
    assert "nothing pending" in early.text and "- sample sample:1:" in early.text
    assert is_enabled(UnitOfWork(owner_conn), "current")

    clock.advance(dt.timedelta(minutes=2))
    late = public_preview(ctx)
    assert len(late.items) == 1 and "1 item(s) pending" in late.text
    assert "- sample sample:1:" not in late.text
    item = late.items[0]
    assert item.key == acknowledged[0].source_ref
    assert item.due_on == acknowledged[0].last_due_on
    assert dict(item.fields) == {
        "floor": "2026-01-01",
        "status": "open",
        "title": acknowledged[0].last_title,
    }
    assert late.digest != early.digest and late.digest[:8] != early.digest[:8]
    assert late.spec_digest == early.spec_digest
    assert rows(owner_conn) == acknowledged
    assert tuple(owner_conn.execute(select(obligation)).mappings()) == database
    assert current_stub.requests == []
    assert is_enabled(UnitOfWork(owner_conn), "current")

    with pytest.raises(SinkRefused, match=CHANGED):
        dispatch(
            "finance.sink.approve",
            {
                "sink": "current",
                "digest_prefix": early.digest[:8],
                "spec_digest": early.spec_digest,
            },
            Role.OWNER,
            ctx=ctx,
        )
    assert approval_count(owner_conn) == approvals
    assert rows(owner_conn) == acknowledged
    fresh = public_preview(ctx)
    assert fresh.digest == late.digest and fresh.spec_digest == late.spec_digest
    accepted = dispatch(
        "finance.sink.approve",
        {
            "sink": "current",
            "digest_prefix": fresh.digest[:8],
            "spec_digest": fresh.spec_digest,
        },
        Role.OWNER,
        ctx=ctx,
    )
    assert isinstance(accepted, Recorded)
    assert approval_count(owner_conn) == approvals + 1
    assert is_enabled(UnitOfWork(owner_conn), "current")
    assert rows(owner_conn) == acknowledged and current_stub.requests == []


@pytest.mark.parametrize("change", ["title", "amount", "due", "status"])
def test_public_current_preview_data_changes_alter_digest_and_code(
    current_context: FinanceContext,
    current_stub: CurrentStub,
    owner_conn: Connection,
    monkeypatch: pytest.MonkeyPatch,
    change: str,
) -> None:
    add(owner_conn)
    ctx = replace(current_context, role=Role.OWNER)
    monkeypatch.setattr(current, "SystemClock", lambda: ctx.clock)
    current.sync_reminders(ctx)
    acknowledged = rows(owner_conn)
    current_stub.requests.clear()
    owner_conn.execute(update(obligation).values(threshold_remaining=Decimal("30.00")))
    early = public_preview(ctx)
    assert len(early.items) == 1
    if change == "title":
        owner_conn.execute(update(payee).values(display_label="Sample Changed Utility"))
    elif change == "amount":
        owner_conn.execute(
            update(obligation).values(threshold_remaining=Decimal("20.00"))
        )
    elif change == "due":
        owner_conn.execute(update(obligation).values(due_date=dt.date(2026, 5, 22)))
    else:
        owner_conn.execute(update(obligation).values(status="superseded"))
    changed = public_preview(ctx)
    assert len(changed.items) == 1
    assert changed.digest != early.digest and changed.digest[:8] != early.digest[:8]
    assert changed.spec_digest == early.spec_digest
    assert rows(owner_conn) == acknowledged and current_stub.requests == []


def test_public_current_preview_rendered_clock_text_keeps_digest_when_membership_fixed(
    current_context: FinanceContext,
    current_stub: CurrentStub,
    owner_conn: Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    add(owner_conn)
    clock = FakeClock(
        dt.datetime(2026, 5, 18, 12, tzinfo=current_context.settings.timezone)
    )
    ctx = replace(current_context, role=Role.OWNER, clock=clock)
    monkeypatch.setattr(current, "SystemClock", lambda: clock)
    early = public_preview(ctx)
    clock.advance(dt.timedelta(days=1))
    late = public_preview(ctx)
    assert len(early.items) == len(late.items) == 1
    assert early.items == late.items
    assert early.text != late.text
    assert "due in 2 day(s)" in early.text and "due in 1 day(s)" in late.text
    assert early.digest == late.digest and early.digest[:8] == late.digest[:8]
    assert late.spec_digest == early.spec_digest
    assert current_stub.requests == [] and rows(owner_conn) == ()


def test_preview_lists_changes_and_floor_without_sending_and_refuses_unset_floor(
    current_context: FinanceContext,
    current_stub: CurrentStub,
    owner_conn: Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    add(owner_conn)
    monkeypatch.setattr(current, "SystemClock", lambda: current_context.clock)
    (preview,) = current.preview_builder(UnitOfWork(owner_conn))
    assert preview.key == "finance:sample-utility:bill-2026-05"
    assert preview.due_on == dt.date(2026, 5, 20)
    assert dict(preview.fields)["floor"] == "2026-01-01"
    assert dict(preview.fields)["status"] == "open"
    assert current_stub.requests == []
    current.sync_reminders(current_context)
    assert current.preview_builder(UnitOfWork(owner_conn)) == []
    monkeypatch.setattr(current, "load_settings", lambda: parse_settings(""))
    with pytest.raises(
        OperationError, match="set finance.reminders.min_due_date first"
    ):
        current.preview_builder(UnitOfWork(owner_conn))


def test_token_rotation_is_read_at_call_time_without_secret_error_text(
    current_context: FinanceContext,
    current_stub: CurrentStub,
    owner_conn: Connection,
) -> None:
    add(owner_conn)
    credential = Path(
        str(current_context.settings.get("finance.reminders.current_credential_file"))
    )
    original = credential.read_text()
    credential.write_text(original.replace("synthetic-token", "synthetic-wrong"))
    result = current.sync_reminders(current_context)
    assert result.state == "failed" and result.reason == "refused"
    assert rows(owner_conn)[0].last_error_code == "refused"
    assert "synthetic-wrong" not in str(result) + str(rows(owner_conn))
    credential.write_text(original)
    assert current.sync_reminders(current_context).state == "synced"
    assert len(current_stub.items) == 1


def test_null_refusal_operation_id_preserves_stale_reread_contract(
    current_context: FinanceContext,
    current_stub: CurrentStub,
    owner_conn: Connection,
) -> None:
    add(owner_conn)
    # Isolate a nullable refusal from succeeded envelopes already accepted by code.
    current_stub.operation_id = "99999999-9999-4999-8999-999999999999"
    assert current.sync_reminders(current_context).state == "synced"
    owner_conn.execute(update(obligation).values(threshold_remaining=Decimal("15.00")))
    current_stub.requests.clear()
    current_stub.stale = 1
    assert current.sync_reminders(current_context).state == "synced"
    assert current_stub.operations() == ["get", "update", "get", "update"]


def test_credential_file_change_stales_approval_without_outbound_request(
    current_context: FinanceContext,
    current_stub: CurrentStub,
    owner_conn: Connection,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    add(owner_conn)
    original = Path(
        str(current_context.settings.get("finance.reminders.current_credential_file"))
    )
    changed = tmp_path / "another-synthetic.env"
    changed.write_text(original.read_text())
    settings = replace(
        current_context.settings,
        values={
            **current_context.settings.values,
            "finance.reminders.current_credential_file": str(changed),
        },
    )
    monkeypatch.setattr(current, "load_settings", lambda: settings)
    assert current.sync_reminders(current_context) == current.SyncResult(
        "pending", "sink_off"
    )
    assert current_stub.requests == [] and rows(owner_conn) == ()


def test_stub_rejects_unexpected_create_fields_without_hiding_them(
    current_context: FinanceContext,
    current_stub: CurrentStub,
    owner_conn: Connection,
) -> None:
    add(owner_conn)
    assert current.sync_reminders(current_context).state == "synced"
    first = next(body for op, body in current_stub.requests if op == "create")
    before = {ref: item.copy() for ref, item in current_stub.items.items()}
    request_count = len(current_stub.requests)
    client = current.make_client(
        UnitOfWork(owner_conn), current.spec(UnitOfWork(owner_conn))
    )
    response = client.request(
        "POST",
        current_stub.url + "/api/v1/operations/current.item.create",
        headers={
            "Authorization": "Bearer synthetic-token",
            "Content-Type": "application/json",
        },
        body=json.dumps(
            {**first, "source_ref": "finance:sample-extra:bill-2026-05", "wake": True}
        ).encode(),
    )
    assert response.status == 422
    assert current_stub.items == before
    assert len(current_stub.requests) == request_count


def test_stub_seen_fingerprint_is_noop_and_new_fingerprint_reopens_done(
    current_context: FinanceContext,
    current_stub: CurrentStub,
    owner_conn: Connection,
) -> None:
    add(owner_conn)
    current.sync_reminders(current_context)
    item = next(iter(current_stub.items.values()))
    item["status"] = "done"
    first = next(body for op, body in current_stub.requests if op == "create")
    client = current.make_client(
        UnitOfWork(owner_conn), current.spec(UnitOfWork(owner_conn))
    )

    def create(payload: dict[str, object]) -> None:
        response = client.request(
            "POST",
            current_stub.url + "/api/v1/operations/current.item.create",
            headers={
                "Authorization": "Bearer synthetic-token",
                "Content-Type": "application/json",
            },
            body=json.dumps(payload).encode(),
        )
        assert response.status == 200

    create({**first, "title": "Changed incoming title", "due_on": "2026-07-01"})
    assert item["status"] == "done" and item["title"] == first["title"]
    create({**first, "fingerprint": "new-fingerprint"})
    assert current_stub.items[item["ref"]]["status"] == "open"
    assert len(current_stub.items) == 1


@pytest.mark.parametrize("change", ["paid", "deleted", "rekeyed"])
def test_committed_claim_visible_before_create_and_recovery_with_fresh_root(
    durable_current_context: FinanceContext,
    scratch_current_engine: Engine,
    current_stub: CurrentStub,
    change: str,
) -> None:
    ctx = durable_current_context
    with scratch_current_engine.begin() as conn:
        add(conn)
    visible: list[bool] = []

    def inspect_claim() -> None:
        with scratch_current_engine.connect() as conn:
            state = conn.execute(select(sink_ticket)).mappings().one_or_none()
            visible.append(
                state is not None
                and state["state"] == "post_pending"
                and bool(state["create_title"])
                and bool(state["create_body"])
                and state["create_due_on"] == dt.date(2026, 5, 20)
            )

    current_stub.before_create = inspect_claim
    current_stub.drop_create = True
    assert current.sync_reminders(ctx).state == "failed"
    assert visible == [True]
    with scratch_current_engine.connect() as conn:
        saved = rows(conn)[0]
        assert (
            saved.state == "post_pending" and saved.last_error_code == "network_error"
        )
        assert saved.external_id is None
    current_stub.before_create = None
    with scratch_current_engine.begin() as conn:
        if change == "paid":
            conn.execute(
                update(obligation).values(
                    status="paid", threshold_remaining=Decimal("0.00")
                )
            )
        elif change == "deleted":
            conn.execute(delete(obligation))
            conn.execute(delete(payee))
        else:
            conn.execute(update(payee).values(slug="sample-renamed"))
    # New _Databases holder, new NullPool engines and a new root invocation.
    from rheo_finance.operations import with_databases

    # Engine URLs are synthetic scratch URLs; never returned or rendered.
    fresh = with_databases(
        replace(ctx),
        owner_url=scratch_current_engine.url,
        reader_url=scratch_current_engine.url,
    )
    assert current.sync_reminders(fresh).state == "synced"
    created = [body for op, body in current_stub.requests if op == "create"]
    original = [body for body in created if body["source_ref"] == saved.source_ref]
    assert len(original) == 2 and original[0] == original[1]
    assert (
        current_stub.items[
            current_stub.identities[("float.obligation", saved.source_ref)]
        ]["status"]
        == "done"
    )
    assert len(current_stub.items) == (2 if change == "rekeyed" else 1)
    assert sum(i["status"] != "done" for i in current_stub.items.values()) == (
        1 if change == "rekeyed" else 0
    )
    with scratch_current_engine.connect() as conn:
        old = next(r for r in rows(conn) if r.source_ref == saved.source_ref)
        assert old.state == "done" and old.last_error_code is None


def test_process_failure_after_claim_preserves_durable_recovery_state(
    durable_current_context: FinanceContext,
    scratch_current_engine: Engine,
    current_stub: CurrentStub,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with scratch_current_engine.begin() as conn:
        add(conn)
    create = current.CurrentSink.create

    class Crash(BaseException):
        pass

    def crash(_sink: current.CurrentSink, _row: SinkTicket) -> current.RemoteItem:
        raise Crash

    monkeypatch.setattr(current.CurrentSink, "create", crash)
    with pytest.raises(Crash):
        current.sync_reminders(durable_current_context)
    with scratch_current_engine.connect() as conn:
        assert rows(conn)[0].state == "post_pending"
    assert current_stub.requests == []
    monkeypatch.setattr(current.CurrentSink, "create", create)
    assert current.sync_reminders(durable_current_context).state == "synced"
    assert current_stub.operations(writes=True) == ["create"]


def test_claim_commit_failure_sends_no_request(
    durable_current_context: FinanceContext,
    scratch_current_engine: Engine,
    current_stub: CurrentStub,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sqlalchemy.engine import RootTransaction
    from sqlalchemy.exc import OperationalError

    with scratch_current_engine.begin() as conn:
        add(conn)
    commit = RootTransaction.commit
    calls = 0

    def failed_commit(transaction: RootTransaction) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:  # Initial complete read commits, then the claim commit fails.
            raise OperationalError("synthetic", {}, Exception("synthetic"))
        commit(transaction)

    monkeypatch.setattr(RootTransaction, "commit", failed_commit)
    with pytest.raises(OperationError, match="^current storage failed$"):
        current.sync_reminders(durable_current_context)
    assert current_stub.requests == []
    with scratch_current_engine.connect() as conn:
        assert rows(conn) == ()


def test_adapter_ignores_incoming_text_and_revalidates_safe_facts(
    current_context: FinanceContext,
    current_stub: CurrentStub,
    owner_conn: Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    add(owner_conn)
    original = desired_tickets

    def altered(
        ctx: FinanceContext, repository: PostgresReminderRepository
    ) -> DesiredOut:
        result = original(ctx, repository)
        return DesiredOut(
            tickets=[
                t.model_copy(update={"title": "private memo", "body": "private memo"})
                for t in result.tickets
            ]
        )

    monkeypatch.setattr(current, "desired_tickets", altered)
    assert current.sync_reminders(current_context).state == "synced"
    assert "private memo" not in str(current_stub.requests)
    assert "Period-key:" in next(iter(current_stub.items.values()))["body"]


def test_actual_invoice_payment_deletion_closes_current_item(
    ledger: Ledger,
    current_context: FinanceContext,
    current_stub: CurrentStub,
) -> None:
    from derive.support import Ledger
    from derive.test_invoice_decisions import DUE_ON, debts, unpaid
    from derive.test_receipts_invoices import CONTRACTOR, INVOICE, invoice, payments

    assert isinstance(ledger, Ledger)
    from derive.test_payment_application import with_settings

    with_settings(ledger, '[finance.reminders]\nmin_due_date = "2026-01-01"\n')
    ledger.file(INVOICE, invoice())
    ledger.derive()
    unpaid(ledger, DUE_ON)
    ledger.derive()
    assert len(debts(ledger)) == 1
    current.sync_reminders(ledger.ctx)
    payments(ledger, ("31-Dec-2025", CONTRACTOR, "750.00"))
    ledger.derive()
    assert debts(ledger) == []
    current_stub.requests.clear()
    current.sync_reminders(ledger.ctx)
    assert current_stub.operations(writes=True) == ["set_status"]
    assert all(i["status"] == "done" for i in current_stub.items.values())


def test_invoice_vendor_registration_closes_old_identity_and_creates_new_once(
    ledger: Ledger,
    current_context: FinanceContext,
    current_stub: CurrentStub,
) -> None:
    from derive.support import Ledger
    from derive.test_invoice_decisions import DUE_ON, open_invoice, unpaid
    from rheo_finance.manual.payees import PAYEES_FILE

    assert isinstance(ledger, Ledger)
    from derive.test_payment_application import with_settings

    with_settings(ledger, '[finance.reminders]\nmin_due_date = "2026-01-01"\n')
    open_invoice(ledger)
    unpaid(ledger, DUE_ON)
    ledger.derive()
    current.sync_reminders(ledger.ctx)
    ledger.manual(PAYEES_FILE, "builder,Sample Builder,vendor,SAMPLE CONTRACTOR,,")
    ledger.derive()
    current_stub.requests.clear()
    current.sync_reminders(ledger.ctx)
    assert current_stub.operations(writes=True).count("create") == 1
    assert current_stub.operations(writes=True).count("set_status") == 1
    assert len(current_stub.items) == 2
    assert sum(i["status"] == "open" for i in current_stub.items.values()) == 1


def test_card_minimum_paid_on_its_due_date_keeps_next_period_open(
    own: Ledger,
    current_context: FinanceContext,
    current_stub: CurrentStub,
) -> None:
    from derive.support import Ledger
    from derive.test_payment_application import (
        card_export,
        paid_card,
        statement_n,
        statement_n1,
        with_settings,
    )

    assert isinstance(own, Ledger)
    own.ctx = replace(own.ctx, settings=current_context.settings)
    with_settings(own, '[finance.reminders]\nmin_due_date = "2025-01-01"\n')
    statement_n(own)
    own.derive()
    current.sync_reminders(own.ctx)
    assert len(current_stub.items) == 1
    minimum = paid_card(dt.date(2025, 3, 25), "25.00")
    card_export(own, minimum)
    own.derive()
    current.sync_reminders(own.ctx)
    assert next(iter(current_stub.items.values()))["status"] == "done"
    statement_n1(own, minimum)
    own.derive()
    current.sync_reminders(own.ctx)
    assert len(current_stub.items) == 2
    assert sum(i["status"] == "open" for i in current_stub.items.values()) == 1
    assert current_stub.operations(writes=True).count("create") == 2


def test_new_settled_period_is_recorded_without_remote_create(
    current_context: FinanceContext,
    current_stub: CurrentStub,
    owner_conn: Connection,
) -> None:
    add(owner_conn, status="paid", threshold="0.00")
    current.sync_reminders(current_context)
    assert current_stub.requests == []
    assert rows(owner_conn)[0].state == "done"
    assert rows(owner_conn)[0].external_id is None


def test_never_created_done_period_acknowledges_later_done_facts(
    current_context: FinanceContext,
    current_stub: CurrentStub,
    owner_conn: Connection,
) -> None:
    add(owner_conn, status="paid", threshold="0.00")
    current.sync_reminders(current_context)
    owner_conn.execute(
        update(obligation).values(
            status="superseded", threshold_remaining=Decimal("20.00")
        )
    )
    assert current.sync_reminders(current_context).state == "synced"
    assert ": 20.00 CAD due " in str(rows(owner_conn)[0].last_title)
    assert current_stub.requests == []


def test_never_created_done_period_can_create_its_first_item_when_reopened(
    current_context: FinanceContext,
    current_stub: CurrentStub,
    owner_conn: Connection,
) -> None:
    add(owner_conn, status="paid", threshold="0.00")
    current.sync_reminders(current_context)
    assert current_stub.requests == []
    owner_conn.execute(
        update(obligation).values(status="open", threshold_remaining=Decimal("25.00"))
    )
    current.sync_reminders(current_context)
    assert current_stub.operations(writes=True) == ["create"]
    assert rows(owner_conn)[0].last_status == "open"
    assert rows(owner_conn)[0].external_id is not None


def test_disabled_and_unconfigured_leave_all_work_pending(
    current_context: FinanceContext,
    current_stub: CurrentStub,
    owner_conn: Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    add(owner_conn)
    monkeypatch.setattr(current, "is_enabled", lambda _u, _s: False)
    assert current.sync_reminders(current_context) == current.SyncResult(
        "pending", "sink_off"
    )
    assert current_stub.requests == [] and rows(owner_conn) == ()
    monkeypatch.setattr(current, "configuration", lambda _s: None)
    assert current.sync_reminders(current_context) == current.SyncResult(
        "pending", "not_configured"
    )
    assert current_stub.requests == [] and rows(owner_conn) == ()


def test_unauthorized_stops_serial_run_and_later_delivers_each_identity_once(
    current_context: FinanceContext,
    current_stub: CurrentStub,
    owner_conn: Connection,
) -> None:
    add(owner_conn, slug="sample-alpha")
    add(owner_conn, slug="sample-zeta")
    current_stub.unauthorized = True
    assert current.sync_reminders(current_context) == current.SyncResult(
        "failed", "unauthorized"
    )
    assert current_stub.operations() == ["create"]
    assert len(rows(owner_conn)) == 1
    assert rows(owner_conn)[0].last_error_code == "unauthorized"
    current_stub.unauthorized = False
    assert current.sync_reminders(current_context).state == "synced"
    assert len(current_stub.items) == 2
    assert len({i["source_ref"] for i in current_stub.items.values()}) == 2
    assert all(r.last_error_code is None for r in rows(owner_conn))


def test_unchanged_desired_keeps_owner_edits_and_sends_no_writes(
    current_context: FinanceContext,
    current_stub: CurrentStub,
    owner_conn: Connection,
) -> None:
    add(owner_conn)
    current.sync_reminders(current_context)
    item = next(iter(current_stub.items.values()))
    item.update(
        {
            "title": "Owner edited",
            "status": "done",
            "due_on": "2026-06-01",
            "priority": "low",
        }
    )
    item["revision"] += 1
    current_stub.requests.clear()
    current.sync_reminders(current_context)
    assert current_stub.operations(writes=True) == []
    assert item["title"] == "Owner edited" and item["status"] == "done"
    owner_conn.execute(update(obligation).values(threshold_remaining=Decimal("20.00")))
    current.sync_reminders(current_context)
    assert current_stub.items[item["ref"]]["status"] == "open"
    assert ": 20.00 CAD due " in item["title"]
    assert current_stub.operations(writes=True) == ["update", "set_status"]
    current_stub.requests.clear()
    current.sync_reminders(current_context)
    assert current_stub.operations(writes=True) == []


@pytest.mark.parametrize("field", ["due", "priority"])
def test_due_and_priority_desired_changes_converge_once(
    current_context: FinanceContext,
    current_stub: CurrentStub,
    owner_conn: Connection,
    field: str,
) -> None:
    add(owner_conn)
    current.sync_reminders(current_context)
    current_stub.requests.clear()
    if field == "due":
        owner_conn.execute(update(obligation).values(due_date=dt.date(2026, 5, 22)))
    else:
        from rheo_finance.clock import FakeClock

        current_context = replace(
            current_context, clock=FakeClock(dt.datetime(2026, 6, 1, tzinfo=dt.UTC))
        )
    current.sync_reminders(current_context)
    assert current_stub.operations(writes=True) == ["update"]
    payload = next(body for op, body in current_stub.requests if op == "update")
    assert ("due_on" in payload) == (field == "due")
    assert ("priority" in payload) == (field == "priority")
    current_stub.requests.clear()
    current.sync_reminders(current_context)
    assert current_stub.operations(writes=True) == []


@pytest.mark.parametrize("stales,expected", [(1, "synced"), (2, "failed")])
def test_revision_cas_rereads_and_retries_only_once(
    current_context: FinanceContext,
    current_stub: CurrentStub,
    owner_conn: Connection,
    stales: int,
    expected: str,
) -> None:
    add(owner_conn)
    current.sync_reminders(current_context)
    before = rows(owner_conn)[0]
    owner_conn.execute(update(obligation).values(threshold_remaining=Decimal("15.00")))
    current_stub.requests.clear()
    current_stub.stale = stales
    assert current.sync_reminders(current_context).state == expected
    assert current_stub.operations() == ["get", "update", "get", "update"]
    if expected == "failed":
        assert rows(owner_conn)[0].last_title == before.last_title
        assert rows(owner_conn)[0].last_error_code == "record_stale"
        assert current.sync_reminders(current_context).state == "synced"
    assert current_stub.operations().count("create") == 0


@pytest.mark.parametrize("http_status", [200, 400, 403, 500])
@pytest.mark.parametrize("operation", ["update", "set_status"])
def test_non_conflict_stale_envelope_stops_without_retry_or_acknowledgement(
    current_context: FinanceContext,
    current_stub: CurrentStub,
    owner_conn: Connection,
    http_status: int,
    operation: str,
) -> None:
    add(owner_conn)
    assert current.sync_reminders(current_context).state == "synced"
    before = rows(owner_conn)[0]
    if operation == "update":
        owner_conn.execute(
            update(obligation).values(threshold_remaining=Decimal("15.00"))
        )
    else:
        owner_conn.execute(delete(obligation))
    current_stub.requests.clear()
    current_stub.stale = 1
    current_stub.stale_http_status = http_status

    assert current.sync_reminders(current_context) == current.SyncResult(
        "failed", "refused"
    )
    assert current_stub.operations() == (
        ["get", "update"] if operation == "update" else ["get", "get", "set_status"]
    )
    after = rows(owner_conn)[0]
    assert (
        after.model_copy(update={"last_error_code": before.last_error_code}) == before
    )


@pytest.mark.parametrize("operation", ["get", "set_status"])
def test_failed_absence_cleanup_preserves_open_acknowledgement(
    current_context: FinanceContext,
    current_stub: CurrentStub,
    owner_conn: Connection,
    operation: str,
) -> None:
    add(owner_conn)
    current.sync_reminders(current_context)
    owner_conn.execute(delete(obligation))
    current_stub.refuse_operation = operation
    assert current.sync_reminders(current_context).state == "failed"
    row = rows(owner_conn)[0]
    assert row.state == "open" and row.last_status == "open"
    assert row.last_error_code == "refused" and row.settled_at is None
    current_stub.refuse_operation = None
    assert current.sync_reminders(current_context).state == "synced"
    assert rows(owner_conn)[0].state == "done"


def test_partial_convergence_does_not_advance_acknowledged_desired_tuple(
    current_context: FinanceContext,
    current_stub: CurrentStub,
    owner_conn: Connection,
) -> None:
    add(owner_conn)
    current.sync_reminders(current_context)
    before = rows(owner_conn)[0]
    owner_conn.execute(
        update(obligation).values(status="paid", threshold_remaining=Decimal("0.00"))
    )
    current_stub.refuse_operation = "set_status"
    assert current.sync_reminders(current_context).state == "failed"
    assert next(iter(current_stub.items.values()))["title"] != before.last_title
    after = rows(owner_conn)[0]
    assert after.last_title == before.last_title and after.last_status == "open"
    assert after.external_id == before.external_id
    current_stub.requests.clear()
    current_stub.refuse_operation = None
    current.sync_reminders(current_context)
    assert current_stub.operations(writes=True) == ["set_status"]


def test_earlier_ack_survives_later_source_failure(
    current_context: FinanceContext,
    current_stub: CurrentStub,
    owner_conn: Connection,
) -> None:
    add(owner_conn, slug="sample-alpha")
    add(owner_conn, slug="sample-zeta")
    current_stub.refuse_source_ref = "finance:sample-zeta:bill-2026-05"
    assert current.sync_reminders(current_context).state == "failed"
    first, last = rows(owner_conn)
    assert first.state == "open" and first.last_success_at is not None
    assert last.state == "post_pending" and last.last_success_at is None
    current_stub.refuse_source_ref = None
    current.sync_reminders(current_context)
    assert len(current_stub.items) == 2
    assert current_stub.operations(writes=True).count("create") == 3


def test_create_ref_only_is_read_before_acknowledgement(
    current_context: FinanceContext,
    current_stub: CurrentStub,
    owner_conn: Connection,
) -> None:
    add(owner_conn)
    current_stub.ref_only = True
    assert current.sync_reminders(current_context).state == "synced"
    assert current_stub.operations() == ["create", "get"]
    assert rows(owner_conn)[0].state == "open"


def test_scan_failure_after_an_eligible_row_is_not_empty_success(
    current_context: FinanceContext,
    current_stub: CurrentStub,
    owner_conn: Connection,
) -> None:
    add(owner_conn)
    current.sync_reminders(current_context)
    owner_conn.execute(update(obligation).values(status="presumed_paid"))
    add(owner_conn, slug="sample-bad", label="Sample 999999")
    current_stub.requests.clear()
    with pytest.raises(OperationError):
        current.sync_reminders(current_context)
    assert current_stub.requests == []
    assert rows(owner_conn)[0].state == "open"
    assert next(iter(current_stub.items.values()))["status"] == "open"


def rows(conn: Connection) -> tuple[SinkTicket, ...]:
    return PostgresCurrentTicketRepository(UnitOfWork(conn)).all()


def test_create_once_partial_full_and_returned_payment(
    current_context: FinanceContext,
    current_stub: CurrentStub,
    owner_conn: Connection,
) -> None:
    add(owner_conn)
    assert current.sync_reminders(current_context).state == "synced"
    assert current_stub.operations(writes=True) == ["create"]
    item = next(iter(current_stub.items.values()))
    assert item["area"] == "bills" and item["provenance"] == "agent"
    assert item["fingerprint"] == item["source_ref"]
    assert "wake" not in item and "snoozed_until" not in item
    assert item["body"].splitlines() == [
        "Type: bill-due",
        "Who/what: Sample Utility, bill-2026-05",
        "Amount/detail: 40.00 CAD",
        "Date: due 2026-05-20",
        "Period-key: finance:sample-utility:bill-2026-05",
        "Note: tracked by Float; paid or settled items are closed automatically",
    ]
    owner_conn.execute(update(obligation).values(threshold_remaining=Decimal("15.00")))
    current.sync_reminders(current_context)
    assert ": 15.00 CAD due " in item["title"]
    owner_conn.execute(
        update(obligation).values(threshold_remaining=Decimal("0.00"), status="paid")
    )
    current.sync_reminders(current_context)
    assert item["status"] == "done"
    owner_conn.execute(
        update(obligation).values(threshold_remaining=Decimal("40.00"), status="open")
    )
    current.sync_reminders(current_context)
    assert current_stub.items[item["ref"]]["status"] == "open"
    assert current_stub.operations(writes=True).count("create") == 1
    assert rows(owner_conn)[0].last_status == "open"


@pytest.mark.parametrize(
    "change",
    ["presumed_paid", "disputed", "floor", "floor-advanced", "undated", "vanished"],
)
def test_successful_desired_omissions_close_previously_synced_items(
    current_context: FinanceContext,
    current_stub: CurrentStub,
    owner_conn: Connection,
    change: str,
) -> None:
    add(owner_conn)
    current.sync_reminders(current_context)
    if change == "vanished":
        owner_conn.execute(delete(obligation))
    elif change == "floor-advanced":
        current_context = replace(
            current_context,
            settings=parse_settings(
                '[finance.reminders]\nmin_due_date = "2026-06-01"\n'
            ),
        )
    else:
        values: dict[str, object] = (
            {"before_floor": True}
            if change == "floor"
            else {"due_date": None}
            if change == "undated"
            else {"status": change}
        )
        owner_conn.execute(update(obligation).values(**values))
    current_stub.requests.clear()
    assert current.sync_reminders(current_context).state == "synced"
    assert current_stub.operations(writes=True) == ["set_status"]
    assert all(i["status"] == "done" for i in current_stub.items.values())
    assert rows(owner_conn)[0].state == "done"
    current_stub.requests.clear()
    current.sync_reminders(current_context)
    assert current_stub.operations(writes=True) == []


def test_failed_desired_calculation_preserves_open_item(
    current_context: FinanceContext,
    current_stub: CurrentStub,
    owner_conn: Connection,
) -> None:
    add(owner_conn)
    current.sync_reminders(current_context)
    current_stub.requests.clear()
    with pytest.raises(
        OperationError, match="set finance.reminders.min_due_date first"
    ):
        current.sync_reminders(replace(current_context, settings=parse_settings("")))
    assert current_stub.requests == []
    assert rows(owner_conn)[0].state == "open"
    assert next(iter(current_stub.items.values()))["status"] == "open"


def test_lost_create_answer_is_recovered_then_paid_item_closed(
    current_context: FinanceContext,
    current_stub: CurrentStub,
    owner_conn: Connection,
) -> None:
    add(owner_conn)
    current_stub.drop_create = True
    assert current.sync_reminders(current_context).state == "failed"
    assert rows(owner_conn)[0].state == "post_pending"
    owner_conn.execute(
        update(obligation).values(status="paid", threshold_remaining=Decimal("0.00"))
    )
    assert current.sync_reminders(current_context).state == "synced"
    assert current_stub.operations(writes=True).count("create") == 2
    assert len(current_stub.items) == 1
    assert next(iter(current_stub.items.values()))["status"] == "done"
    assert rows(owner_conn)[0].external_id is not None
