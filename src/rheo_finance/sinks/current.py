"""Current's gated operation client and durable desired-state reconciliation."""

from __future__ import annotations

import datetime as dt
import json
import re
import shlex
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy.exc import SQLAlchemyError

from rheo_finance.clock import SystemClock
from rheo_finance.context import FinanceContext, FinancePaths, UnitOfWork
from rheo_finance.contracts import Role
from rheo_finance.http import Client, RequestFailed
from rheo_finance.operations import OperationError, owner_transaction
from rheo_finance.reminders.desired import DesiredTicket, desired_tickets
from rheo_finance.reminders.policy import (
    PayloadPolicy,
    PayloadRefused,
    build_payload,
    safe_token,
)
from rheo_finance.reminders.store import PostgresReminderRepository
from rheo_finance.settings import Settings, SettingsError, load_settings
from rheo_finance.sinks.current_store import (
    CreateSnapshot,
    PostgresCurrentTicketRepository,
    SinkTicket,
    Status,
)
from rheo_finance.sinks.gate import (
    AllowlistRefused,
    HostRefused,
    SinkDisabled,
    is_enabled,
)
from rheo_finance.sinks.preview import PREVIEW_BUILDERS, PreviewItem, SinkPreview
from rheo_finance.sinks.spec import SINK_SPECS, RequestTemplate, SinkSpec

SINK = "current"
OPERATIONS = ("create", "get", "update", "set_status")
REQUESTS = tuple(
    RequestTemplate(method="POST", path=f"/api/v1/operations/current.item.{op}")
    for op in OPERATIONS
)
ALLOWLIST = tuple((r.method, r.path) for r in REQUESTS)
_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


class CurrentError(Exception):
    """Fixed local codes only; remote text and HTTP exception text never escape."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, repr=False)
class Configuration:
    base_url: str
    host: str


def _env(settings: Settings) -> dict[str, str]:
    filename = settings.get("finance.reminders.current_credential_file")
    if not isinstance(filename, str):
        return {}
    failure = False
    variables: dict[str, str] = {}
    try:
        for line in (
            Path(filename).expanduser().read_text(encoding="utf-8").splitlines()
        ):
            words = shlex.split(line, comments=True)
            if not words:
                continue
            if len(words) != 2 or words[0] != "export":
                raise ValueError
            name, equals, value = words[1].partition("=")
            if not equals or not _ENV_NAME.fullmatch(name) or name in variables:
                raise ValueError
            variables[name] = value
    except (OSError, UnicodeError, ValueError):
        failure = True
    if failure:
        raise CurrentError("configuration_error")
    return variables


def configuration(settings: Settings) -> Configuration | None:
    base = settings.get("finance.reminders.current_base_url")
    if not isinstance(base, str):
        base = _env(settings).get("CURRENT_API_URL")
    if not base:
        return None
    failure = False
    host = ""
    try:
        parts = urlsplit(base)
        if (
            parts.scheme not in {"http", "https"}
            or not parts.hostname
            or parts.username is not None
            or parts.password is not None
            or parts.path not in {"", "/"}
            or parts.query
            or parts.fragment
        ):
            raise ValueError
        host = parts.hostname.lower()
        if parts.port is not None:
            host += f":{parts.port}"
    except ValueError:
        failure = True
    if failure:
        raise CurrentError("configuration_error")
    return Configuration(base.rstrip("/"), host)


def spec(_uow: UnitOfWork) -> SinkSpec:
    settings = load_settings()
    try:
        config = configuration(settings)
    except CurrentError:
        # Unavailable outbound configuration must not block local derivation.
        # No host is allowed until configuration can be resolved again.
        config = None
    credential = settings.get("finance.reminders.current_credential_file")
    failure = False
    try:
        declared = SinkSpec(
            name=SINK,
            hosts=(config.host,) if config else (),
            requests=REQUESTS,
            key_file=credential if isinstance(credential, str) else None,
            payload_policy_version=1,
            data_inputs=(
                ("token_var", settings.get_str("finance.reminders.current_token_var")),
            ),
        )
    except ValidationError:
        failure = True
    if failure:
        raise CurrentError("configuration_error")
    return declared


def https_client(uow: UnitOfWork, declared: SinkSpec) -> Client:
    return Client(SINK, declared, ALLOWLIST, uow=uow)


make_client: Callable[[UnitOfWork, SinkSpec], Client] = https_client


class RemoteItem(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True, strict=True)
    ref: str = Field(min_length=1)
    revision: int = Field(ge=1)
    status: Literal["open", "waiting", "snoozed", "done"]
    title: str
    due_on: dt.date | None
    priority: Literal["critical", "high", "normal", "low"]


class RemoteError(BaseModel):
    model_config = ConfigDict(extra="ignore")
    error_code: str


class Envelope(BaseModel):
    model_config = ConfigDict(extra="ignore")
    state: str
    operation_id: str | None
    result: dict[str, object] | None = None
    error: RemoteError | None = None


class CurrentSink:
    def __init__(self, uow: UnitOfWork) -> None:
        self.uow = uow

    def _call(self, operation: str, inputs: dict[str, object]) -> dict[str, object]:
        if operation not in OPERATIONS:
            raise CurrentError("protocol_error")
        settings = load_settings()
        config = configuration(settings)
        if config is None:
            raise CurrentError("not_configured")
        name = settings.get_str("finance.reminders.current_token_var")
        if not _ENV_NAME.fullmatch(name):
            raise CurrentError("configuration_error")
        token = _env(settings).get(name)
        if not token:
            raise CurrentError("not_configured")
        error: str | None = None
        result: dict[str, object] | None = None
        try:
            client = make_client(self.uow, spec(self.uow))
            response = client.request(
                "POST",
                f"{config.base_url}/api/v1/operations/current.item.{operation}",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                },
                body=json.dumps(inputs).encode("utf-8"),
            )
            if response.status == 401:
                error = "unauthorized"
            else:
                try:
                    envelope = Envelope.model_validate_json(response.body)
                except ValidationError:
                    if response.status == 200:
                        raise
                    error = "refused"
                else:
                    if (
                        response.status == 200
                        and envelope.state == "succeeded"
                        and envelope.result is not None
                    ):
                        result = envelope.result
                    elif envelope.error is not None:
                        error = (
                            "record_stale"
                            if response.status == 409
                            and envelope.error.error_code == "record_stale"
                            else "refused"
                        )
                    else:
                        error = "protocol_error"
        except SinkDisabled:
            error = "sink_off"
        except (AllowlistRefused, HostRefused):
            error = "request_refused"
        except RequestFailed:
            error = "network_error"
        except (ValidationError, ValueError):
            error = "protocol_error"
        if error:
            raise CurrentError(error)
        if result is None:
            raise CurrentError("protocol_error")
        return result

    def _item(self, operation: str, inputs: dict[str, object]) -> RemoteItem:
        result = self._call(operation, inputs)
        raw = result.get("item", result)
        failure = False
        try:
            # JSON validation admits ISO dates while preserving strict numeric types.
            item = RemoteItem.model_validate_json(json.dumps(raw))
        except (ValidationError, ValueError, TypeError):
            failure = True
        if failure:
            raise CurrentError("protocol_error")
        return item

    def get(self, ref: str) -> RemoteItem:
        return self._item("get", {"ref": ref})

    def create(self, row: SinkTicket) -> RemoteItem:
        result = self._call(
            "create",
            {
                **row.create.model_dump(mode="json"),
                "area": "bills",
                "provenance": "agent",
                "source_kind": "float.obligation",
                "source_ref": row.source_ref,
                "fingerprint": row.source_ref,
            },
        )
        raw = result.get("item")
        if raw is None:
            ref = result.get("ref")
            if not isinstance(ref, str) or not ref:
                raise CurrentError("protocol_error")
            return self.get(ref)
        failure = False
        try:
            item = RemoteItem.model_validate_json(json.dumps(raw))
        except (ValidationError, ValueError, TypeError):
            failure = True
        if failure:
            raise CurrentError("protocol_error")
        return item

    def mutate(
        self,
        operation: str,
        ref: str,
        fields: dict[str, object],
        fresh: RemoteItem | None = None,
    ) -> RemoteItem:
        item = fresh if fresh is not None else self.get(ref)
        for attempt in range(2):
            try:
                return self._item(
                    operation, {"ref": ref, "revision": item.revision, **fields}
                )
            except CurrentError as exc:
                if exc.code != "record_stale" or attempt:
                    raise
            item = self.get(ref)
        raise CurrentError("record_stale")


@dataclass(frozen=True)
class Target:
    create: CreateSnapshot
    status: Status


def target(ticket: DesiredTicket, registered: frozenset[str]) -> Target:
    """Revalidate safe facts through policy62; never forward its incoming text."""
    fields = ticket.source_ref.split(":")
    if (
        len(fields) != 3
        or fields[0] != "finance"
        or not safe_token(fields[1])
        or fields[2] != ticket.period
    ):
        raise PayloadRefused("invalid reminder source key")
    policy: PayloadPolicy | None = None
    try:
        policy = PayloadPolicy(
            payee_label=ticket.payee_label,
            amount=ticket.amount,
            due_date=ticket.due_date,
            period=ticket.period,
        )
    except ValidationError:
        pass  # Raise outside the exception context, without rejected facts.
    if policy is None:
        raise PayloadRefused("invalid reminder payload policy")
    validated = build_payload(policy, registered_payees=registered)
    amount = f"{validated.amount:.2f} CAD"
    body = (
        f"Type: bill-due\nWho/what: {validated.payee_label}, {validated.period}\n"
        f"Amount/detail: {amount}\nDate: due {validated.due_date.isoformat()}\n"
        f"Period-key: {ticket.source_ref}\n"
        "Note: tracked by Float; paid or settled items are closed automatically"
    )
    snapshot: CreateSnapshot | None = None
    try:
        snapshot = CreateSnapshot(
            title=validated.title,
            body=body,
            due_on=validated.due_date,
            priority=ticket.severity,
        )
    except ValidationError:
        pass  # Length/shape errors must not repeat restricted payload facts.
    if snapshot is None:
        raise PayloadRefused("invalid Current reminder payload")
    return Target(snapshot, ticket.status)


def _absent(row: SinkTicket) -> Target:
    return Target(
        row.create.model_copy(
            update={
                "title": row.last_title or row.create.title,
                "due_on": row.last_due_on or row.create.due_on,
                "priority": row.last_priority or row.create.priority,
            }
        ),
        "done",
    )


def _same(row: SinkTicket, wanted: Target) -> bool:
    return (row.last_title, row.last_due_on, row.last_priority, row.last_status) == (
        wanted.create.title,
        wanted.create.due_on,
        wanted.create.priority,
        wanted.status,
    )


@dataclass(frozen=True)
class SyncResult:
    state: Literal["synced", "pending", "failed"]
    reason: str | None = None


def sync_reminders(ctx: FinanceContext) -> SyncResult:
    """Job root, outside dispatch; short transactions commit around each request."""
    failed = False
    try:
        return _sync(ctx)
    except SQLAlchemyError:
        failed = True
    if failed:
        raise OperationError("current storage failed")
    raise OperationError("current sync failed")


def _sync(ctx: FinanceContext) -> SyncResult:
    with owner_transaction(ctx) as uow:
        facts = PostgresReminderRepository(uow)
        desired = desired_tickets(ctx, facts)
        registered = facts.registered_payees()
        wanted = {t.source_ref: target(t, registered) for t in desired.tickets}
        if len(wanted) != len(desired.tickets):
            raise PayloadRefused("duplicate reminder source keys")
        existing = {r.source_ref: r for r in PostgresCurrentTicketRepository(uow).all()}
        try:
            settings = load_settings()
            if configuration(settings) is None:
                return SyncResult("pending", "not_configured")
            credential = settings.get("finance.reminders.current_credential_file")
            if not isinstance(credential, str):
                return SyncResult("pending", "not_configured")
            if not is_enabled(uow, SINK):
                return SyncResult("pending", "sink_off")
        except (CurrentError, SettingsError):
            return SyncResult("pending", "configuration_error")
    for ref in sorted(wanted.keys() | existing.keys()):
        intent = wanted.get(ref) or _absent(existing[ref])
        failure: str | None = None
        try:
            _reconcile(ctx, ref, intent)
        except CurrentError as exc:
            failure = exc.code
        if failure is not None:
            with owner_transaction(ctx) as uow:
                store = PostgresCurrentTicketRepository(uow)
                row = store.get(ref)
                if row is not None:
                    store.save(
                        row.model_copy(
                            update={
                                "last_error_code": failure,
                                "last_attempt_at": ctx.clock.now_utc(),
                            }
                        )
                    )
            return SyncResult(
                "pending" if failure in {"sink_off", "not_configured"} else "failed",
                failure,
            )
    return SyncResult("synced")


def _acknowledged(row: SinkTicket, intent: Target, now: dt.datetime) -> SinkTicket:
    return row.model_copy(
        update={
            "state": intent.status,
            "last_title": intent.create.title,
            "last_due_on": intent.create.due_on,
            "last_priority": intent.create.priority,
            "last_status": intent.status,
            "last_success_at": now,
            "last_attempt_at": now,
            "last_error_code": None,
            "settled_at": now if intent.status == "done" else None,
        }
    )


def _reconcile(ctx: FinanceContext, ref: str, intent: Target) -> None:
    now = ctx.clock.now_utc()
    # Claim transaction A always exits before any create. It is not a worker lock:
    # Current's source pair and fingerprint also fold overlapping create attempts.
    with owner_transaction(ctx) as uow:
        store = PostgresCurrentTicketRepository(uow)
        row = store.get(ref)
        if row is None:
            candidate = SinkTicket(
                source_ref=ref,
                state="post_pending" if intent.status == "open" else "done",
                create=intent.create,
                last_attempt_at=now,
            )
            if intent.status == "done":
                candidate = _acknowledged(candidate, intent, now)
            store.insert_if_absent(candidate)
            row = store.get(ref)
            assert row is not None
        if row.external_id is None and row.state == "done" and intent.status == "open":
            # This period was first seen settled and has never had a remote create.
            row = row.model_copy(
                update={"state": "post_pending", "create": intent.create}
            )
            store.save(row)
        if _same(row, intent) and row.state != "post_pending":
            return
    fresh: RemoteItem | None = None
    if row.state == "post_pending":
        with owner_transaction(ctx) as uow:
            fresh = CurrentSink(uow).create(row)
            row = row.model_copy(update={"external_id": fresh.ref, "state": "open"})
            PostgresCurrentTicketRepository(uow).save(row)
    ref_ = row.external_id
    if ref_ is None:
        with owner_transaction(ctx) as uow:
            PostgresCurrentTicketRepository(uow).save(_acknowledged(row, intent, now))
        return
    # Each mutation gets a fresh read; only a just-returned create can supply it.
    with owner_transaction(ctx) as uow:
        sink = CurrentSink(uow)
        remote = fresh if fresh is not None else sink.get(ref_)
        fields: dict[str, object] = {}
        if remote.title != intent.create.title:
            fields["title"] = intent.create.title
        if remote.due_on != intent.create.due_on:
            fields["due_on"] = intent.create.due_on.isoformat()
        if remote.priority != intent.create.priority:
            fields["priority"] = intent.create.priority
        if fields:
            remote = sink.mutate("update", ref_, fields, fresh=remote)
    if remote.status != intent.status:
        with owner_transaction(ctx) as uow:
            remote = CurrentSink(uow).mutate(
                "set_status",
                ref_,
                {"status": intent.status},
                fresh=fresh if not fields else None,
            )
    if (remote.title, remote.due_on, remote.priority, remote.status) != (
        intent.create.title,
        intent.create.due_on,
        intent.create.priority,
        intent.status,
    ):
        raise CurrentError("incomplete_convergence")
    with owner_transaction(ctx) as uow:
        store = PostgresCurrentTicketRepository(uow)
        persisted = store.get(ref)
        assert persisted is not None
        store.save(_acknowledged(persisted, intent, now))


def preview_builder(uow: UnitOfWork) -> list[PreviewItem]:
    """Exact pending changes now, including clock-driven overdue priority.

    Current alone may change preview membership/code across the local due-date
    boundary. Typed fields exclude rendered clock text; see ADR-0015.
    """
    settings = load_settings()
    ctx = FinanceContext(
        settings=settings,
        clock=SystemClock(),
        paths=FinancePaths(Path(".")),
        role=Role.OWNER,
        snapshot_provider=lambda _u, _r: None,
    )
    facts = PostgresReminderRepository(uow)
    wanted = {
        t.source_ref: target(t, facts.registered_payees())
        for t in desired_tickets(ctx, facts).tickets
    }
    rows = {r.source_ref: r for r in PostgresCurrentTicketRepository(uow).all()}
    items = []
    for ref in sorted(wanted.keys() | rows.keys()):
        row = rows.get(ref)
        intent = wanted.get(ref) or _absent(rows[ref])
        if row is not None and _same(row, intent):
            continue
        items.append(
            PreviewItem(
                kind="reminder",
                key=ref,
                due_on=intent.create.due_on,
                fields=(
                    ("title", intent.create.title),
                    ("status", intent.status),
                    ("floor", str(settings.get_date("finance.reminders.min_due_date"))),
                ),
            )
        )
    return items


SINK_SPECS[SINK] = spec
PREVIEW_BUILDERS[SINK] = SinkPreview(items=preview_builder)
