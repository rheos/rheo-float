"""The sink operations: ``finance.sink.state|preview|approve|disable`` (FR-65).

- ``state`` (READ, reader connection) reads ``report_sink`` and ``report_sink_spec``
  only, and adds the ``enabled`` setting from the settings file.
- ``preview`` (READ, owner connection) builds what the sink would send or request and
  its clock-free digest, and sends nothing.
- ``approve`` (MUTATE, owner only) recomputes the preview and the spec in its own
  transaction and records a ``sink_approve`` decision only when the preview digest
  still starts with the typed prefix and the spec digest is still the one the preview
  showed. ``decision.record`` refuses that kind, so this module writes the row itself.
  ``cli.approve`` is its one caller, and it runs only in a terminal.
- ``disable`` (MUTATE, owner only) records ``sink_disable``; it needs no terminal.

Both writes also refresh the derived ``sink_spec`` table, so ``report_sink`` is
current right after them rather than after the next derive. A disable is recorded
first and refreshes only its sink's row, in a savepoint: a provider that fails cannot
roll the disable back (FR-65, disable at any time); the output says when the refresh
did not happen.

The enabled setting is read from ``$FINANCE_DATA_DIR/settings.toml`` at each call, as
the gate reads it, so what these report is what the gate sees. No operation here sets
it.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import insert, select

from rheo_finance.context import FinanceContext, UnitOfWork
from rheo_finance.contracts import Role, SafetyClass, Tiered
from rheo_finance.decision import DECISION_KIND_COLUMNS, DecisionKind
from rheo_finance.operations import OperationError, OperationSpec, register
from rheo_finance.settings import SINKS, load_settings
from rheo_finance.sinks.preview import PREVIEW_BUILDERS, PreviewItem, build_preview
from rheo_finance.sinks.spec import current_spec, spec_digest
from rheo_finance.sinks.store import write_sink_spec, write_sink_specs
from rheo_finance.storage.ids import uuid7
from rheo_finance.storage.schema import decision, report_sink, report_sink_spec
from rheo_finance.tier import Tier

P, R = Tier.PUBLIC, Tier.RESTRICTED
CHANGED = "pending items changed, run approve again"


class SinkRefused(OperationError):
    """A sink operation the rules refuse. Messages name the sink, never a value."""


def _known_sink(value: str) -> str:
    if value not in SINKS:
        raise ValueError(f"a sink is one of {', '.join(SINKS)}")
    return value


class SinkIn(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    sink: str

    _sink = field_validator("sink")(_known_sink)


class ApproveIn(SinkIn):
    """The sink, the first 8 hex of the preview digest the owner typed, and the spec
    digest of that preview (passed by ``cli.approve``, not typed)."""

    digest_prefix: Annotated[str, Field(pattern=r"^[0-9a-f]{8}$")]
    # None when the preview showed no spec: the handler then refuses, as it does for a
    # sink with nothing to approve.
    spec_digest: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")] | None


class StateIn(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


Approval = Literal["none", "current", "stale"]


class SinkState(BaseModel):
    model_config = ConfigDict(frozen=True)

    sink: Annotated[str, Tiered(P)]
    setting_enabled: Annotated[bool, Tiered(P)]
    approval: Annotated[Approval, Tiered(P)]
    approved_at: Annotated[dt.datetime | None, Tiered(P)]
    # The approval's digest, and the current spec's from ``report_sink_spec``.
    approved_spec_digest: Annotated[str | None, Tiered(P)]
    spec_digest: Annotated[str | None, Tiered(P)]
    # The setting is true and the approval current: what the gate will also find,
    # once it recomputes the spec digest in code.
    enabled: Annotated[bool, Tiered(P)]


class StateOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    sinks: Annotated[list[SinkState], Tiered(P)]


class Preview(BaseModel):
    model_config = ConfigDict(frozen=True)

    sink: Annotated[str, Tiered(P)]
    items: Annotated[list[PreviewItem], Tiered(R)]
    text: Annotated[str, Tiered(R)]
    digest: Annotated[str, Tiered(P)]
    # The digest of the spec the preview showed; None when the sink declares none.
    spec_digest: Annotated[str | None, Tiered(P)]
    requests: Annotated[list[str], Tiered(P)]
    key_file: Annotated[str | None, Tiered(P)]
    setting_enabled: Annotated[bool, Tiered(P)]


class Recorded(BaseModel):
    model_config = ConfigDict(frozen=True)

    decision_id: Annotated[uuid.UUID, Tiered(P)]
    sink: Annotated[str, Tiered(P)]
    setting_enabled: Annotated[bool, Tiered(P)]
    # False when the sink_spec refresh failed; the next derive rewrites the row.
    spec_refreshed: Annotated[bool, Tiered(P)] = True


def _setting(sink: str) -> bool:
    return load_settings().sink_enabled_setting(sink)


# --- finance.sink.state ---------------------------------------------------------------


def state(_ctx: FinanceContext, uow: UnitOfWork, _data: StateIn) -> StateOut:
    approvals = {row.sink: row for row in uow.execute(select(report_sink))}
    specs = {
        row.sink: row.spec_digest
        for row in uow.execute(
            select(report_sink_spec.c.sink, report_sink_spec.c.spec_digest)
        )
    }
    settings = load_settings()
    sinks = []
    for sink in SINKS:
        row = approvals.get(sink)
        approval: Approval = "none"
        if row is not None and row.approved_at is not None:
            approval = "stale" if row.stale else "current"
        setting = settings.sink_enabled_setting(sink)
        sinks.append(
            SinkState(
                sink=sink,
                setting_enabled=setting,
                approval=approval,
                approved_at=None if row is None else row.approved_at,
                approved_spec_digest=None if row is None else row.spec_digest,
                spec_digest=specs.get(sink),
                enabled=setting and approval == "current",
            )
        )
    return StateOut(sinks=sinks)


# --- finance.sink.preview -------------------------------------------------------------


def preview(ctx: FinanceContext, uow: UnitOfWork, data: SinkIn) -> Preview:
    source = PREVIEW_BUILDERS.get(data.sink)
    if source is None:
        raise SinkRefused(f"{data.sink}: this build has no preview for the sink yet")
    spec = current_spec(uow, data.sink)
    setting = _setting(data.sink)
    built = build_preview(ctx, uow, data.sink, source, spec, setting_enabled=setting)
    return Preview(
        sink=data.sink,
        items=built.items,
        text=built.report.text(),
        digest=built.digest,
        spec_digest=None if spec is None else spec_digest(spec),
        requests=[] if spec is None else [each.as_line() for each in spec.requests],
        key_file=None if spec is None else spec.key_file,
        setting_enabled=setting,
    )


# --- finance.sink.approve and finance.sink.disable ------------------------------------


def _write(
    ctx: FinanceContext, uow: UnitOfWork, kind: DecisionKind, values: dict[str, str]
) -> uuid.UUID:
    """Insert one sink decision. ``decision.record`` refuses ``sink_approve`` by
    design, so the sink operations write their own rows; migration 0002's CHECK holds
    the columns too."""
    rules = DECISION_KIND_COLUMNS[kind]
    if set(values) != set(rules.required):
        raise SinkRefused(f"{kind.value}: needs exactly {', '.join(rules.required)}")
    now = ctx.clock.now_utc()
    decision_id = uuid7(now)
    uow.execute(
        insert(decision).values(
            id=decision_id, kind=kind.value, decided_at=now, **values
        )
    )
    return decision_id


def approve(ctx: FinanceContext, uow: UnitOfWork, data: ApproveIn) -> Recorded:
    source = PREVIEW_BUILDERS.get(data.sink)
    spec = current_spec(uow, data.sink)
    if source is None or spec is None:
        raise SinkRefused(
            f"{data.sink}: this build declares no spec or preview for the sink, so"
            " there is nothing to approve"
        )
    setting = _setting(data.sink)
    built = build_preview(ctx, uow, data.sink, source, spec, setting_enabled=setting)
    current = spec_digest(spec)
    if not built.digest.startswith(data.digest_prefix) or current != data.spec_digest:
        raise SinkRefused(f"{data.sink}: {CHANGED}")
    write_sink_specs(uow)
    decision_id = _write(
        ctx,
        uow,
        DecisionKind.SINK_APPROVE,
        {
            "sink": data.sink,
            "spec_digest": current,
            "preview_digest": built.digest,
        },
    )
    return Recorded(decision_id=decision_id, sink=data.sink, setting_enabled=setting)


def disable(ctx: FinanceContext, uow: UnitOfWork, data: SinkIn) -> Recorded:
    decision_id = _write(ctx, uow, DecisionKind.SINK_DISABLE, {"sink": data.sink})
    refreshed = True
    try:
        with uow.savepoint():
            write_sink_spec(uow, data.sink)
    except Exception:
        # The disable stands whatever the provider does; the refresh is cosmetic
        # (report_sink reads a disabled sink as not approved without it), and the
        # output reports it rather than letting it vanish.
        refreshed = False
    return Recorded(
        decision_id=decision_id,
        sink=data.sink,
        setting_enabled=_setting(data.sink),
        spec_refreshed=refreshed,
    )


register(
    OperationSpec(
        name="finance.sink.state",
        input_model=StateIn,
        output_model=StateOut,
        handler=state,
        safety_class=SafetyClass.READ,
        roles=frozenset({Role.OWNER, Role.MEMBER, Role.SERVICE}),
        connection="reader",
        tool_name="finance_sinks",
        description=(
            "Per sink: the enabled setting, when it was approved, and whether that"
            " approval is current or stale, from the report views."
        ),
    )
)
register(
    OperationSpec(
        name="finance.sink.preview",
        input_model=SinkIn,
        output_model=Preview,
        handler=preview,
        safety_class=SafetyClass.READ,
        roles=frozenset({Role.OWNER, Role.SERVICE}),
        connection="owner",
        description=(
            "What a sink would send or request now, with its clock-free digest."
            " Sends nothing."
        ),
    )
)
register(
    OperationSpec(
        name="finance.sink.approve",
        input_model=ApproveIn,
        output_model=Recorded,
        handler=approve,
        safety_class=SafetyClass.MUTATE,
        roles=frozenset({Role.OWNER}),
        connection="owner",
        owner_only=True,
        description=(
            "Record the owner's approval of a sink against the preview digest they"
            " typed. Reached only from `finance sink approve` in a terminal."
        ),
    )
)
register(
    OperationSpec(
        name="finance.sink.disable",
        input_model=SinkIn,
        output_model=Recorded,
        handler=disable,
        safety_class=SafetyClass.MUTATE,
        roles=frozenset({Role.OWNER}),
        connection="owner",
        description="Record a sink_disable decision; the next tick sends nothing.",
    )
)
