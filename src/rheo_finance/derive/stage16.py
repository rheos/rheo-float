"""Stage 16: deterministic facts, pending notice state and actual overdrawn spells.

Mail intake will supply ReadyLoader after it stores statement-ready mail. It must
fingerprint that input table; output notices are not mail and cannot feed this seam.
Statement identity is (account, period), independent of when a tick first sees it.
The availability lag is global until the mail intake account configuration lands.
"""

from __future__ import annotations

import calendar
import datetime as dt
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Annotated, Final

from pydantic import BaseModel, ConfigDict
from sqlalchemy import insert, select
from sqlalchemy.dialects.postgresql import insert as upsert

from rheo_finance.clock import local_today
from rheo_finance.context import FinanceContext, UnitOfWork
from rheo_finance.contracts import Tiered
from rheo_finance.events import record_event
from rheo_finance.manual.stage2 import REGISTRY_DATE
from rheo_finance.storage.schema import (
    account,
    document,
    event,
    notice,
    obligation,
    overdrawn_spell,
    pair_edge,
    parsed_overdrawn,
    payment,
    payment_evidence,
    statement_period,
    txn,
)
from rheo_finance.tier import Tier

PAYMENT_REVERSED: Final = "finance.payment.reversed"
OBLIGATION_CREATED: Final = "finance.obligation.created"
OBLIGATION_SUPERSEDED: Final = "finance.obligation.superseded"
STATEMENT_OVERDUE: Final = "finance.statement.went_overdue"
STATEMENT_AVAILABLE: Final = "finance.statement.became_available"
GRACE_KEY: Final = "finance.statements.grace_days"
LAG_KEY: Final = "finance.statements.availability_lag_days"
# This is the alert-class list for the types built so far (FR-27 + ready amendment).
ALERT_TYPES: Final = frozenset(
    {
        "finance.document.rejected",
        "finance.payment.detected",
        STATEMENT_OVERDUE,
        STATEMENT_AVAILABLE,
    }
)


class PaymentReversed(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    payee_ref: Annotated[str, Tiered(Tier.PUBLIC)]
    reversal_ref: Annotated[str, Tiered(Tier.PUBLIC)]


class ObligationCreated(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    payee_ref: Annotated[str, Tiered(Tier.PUBLIC)]


class ObligationSuperseded(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    replacement_ref: Annotated[str, Tiered(Tier.PUBLIC)]


class StatementOverdue(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    last_period_end: Annotated[dt.date, Tiered(Tier.PUBLIC)]


class StatementAvailable(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    period_end: Annotated[dt.date, Tiered(Tier.PUBLIC)]


EVENT_MODELS: Final[Mapping[str, type[BaseModel]]] = MappingProxyType(
    {
        PAYMENT_REVERSED: PaymentReversed,
        OBLIGATION_CREATED: ObligationCreated,
        OBLIGATION_SUPERSEDED: ObligationSuperseded,
        STATEMENT_OVERDUE: StatementOverdue,
        STATEMENT_AVAILABLE: StatementAvailable,
    }
)


@dataclass(frozen=True)
class StatementReady:
    account_id: uuid.UUID
    period_end: dt.date
    ready_on: dt.date


ReadyLoader = Callable[[UnitOfWork], Sequence[StatementReady]]


def load_ready(_uow: UnitOfWork) -> Sequence[StatementReady]:
    """No mail input exists yet. Phase 5 supplies registered-account mail facts."""
    return ()


def statement_ready_text(label: str) -> str:
    """Resolve only a public label at rendering time, never an account identifier."""
    return f"{label} statement is ready to download"


def next_period(last: dt.date, cadence: str) -> dt.date:
    """Advance by calendar cadence, keeping month-end periods at month end."""
    months = {"monthly": 1, "quarterly": 3, "yearly": 12}[cadence]
    total = last.year * 12 + last.month - 1 + months
    year, month = divmod(total, 12)
    month += 1
    end = calendar.monthrange(year, month)[1]
    day = (
        end
        if last.day == calendar.monthrange(last.year, last.month)[1]
        else min(last.day, end)
    )
    return dt.date(year, month, day)


def _notice(
    uow: UnitOfWork, key: str, kind: str, day: dt.date, now: dt.datetime
) -> None:
    uow.execute(
        upsert(notice)
        .values(
            key=key,
            kind=kind,
            event_date=day,
            first_seen_at=now,
            state="pending",
        )
        .on_conflict_do_nothing(index_elements=[notice.c.key])
    )


def _obligations(uow: UnitOfWork, today: dt.date, now: dt.datetime) -> None:
    for row in uow.execute(select(obligation)):
        ref = f"finance.obligation:{row.id}"
        day = row.issued_on or row.statement_date or row.due_date or REGISTRY_DATE
        record_event(
            uow,
            EVENT_MODELS,
            OBLIGATION_CREATED,
            ref,
            day,
            ObligationCreated(payee_ref=f"finance.payee:{row.payee_id}"),
            identity_on=REGISTRY_DATE,
        )
        if row.supersedes_id is not None:
            record_event(
                uow,
                EVENT_MODELS,
                OBLIGATION_SUPERSEDED,
                f"finance.obligation:{row.supersedes_id}",
                day,
                ObligationSuperseded(replacement_ref=ref),
                key=(row.id,),
                identity_on=REGISTRY_DATE,
            )
        # Presumed paid retains its figures; the status and floor both govern notices.
        if (
            row.status in ("open", "partial")
            and not row.before_floor
            and row.threshold_remaining > 0
            and row.due_date is not None
            and row.due_date < today
        ):
            _notice(uow, f"overdue:{row.id}", "overdue", row.due_date, now)


def _reversals(uow: UnitOfWork) -> None:
    rows = uow.execute(
        select(
            payment.c.id,
            payment.c.payee_id,
            txn.c.id.label("reversal_id"),
            txn.c.posting_date,
        )
        .select_from(
            payment.join(
                payment_evidence, payment.c.id == payment_evidence.c.payment_id
            )
            .join(pair_edge, pair_edge.c.b_txn_id == payment_evidence.c.txn_id)
            .join(txn, txn.c.id == pair_edge.c.a_txn_id)
        )
        .where(
            payment.c.state == "reversed",
            payment_evidence.c.role == "money",
            pair_edge.c.kind == "reversal",
            pair_edge.c.applied,
        )
    )
    for row in rows:
        record_event(
            uow,
            EVENT_MODELS,
            PAYMENT_REVERSED,
            f"finance.payment:{row.id}",
            row.posting_date,
            PaymentReversed(
                payee_ref=f"finance.payee:{row.payee_id}",
                reversal_ref=f"finance.txn:{row.reversal_id}",
            ),
        )


def _statements(
    uow: UnitOfWork, ctx: FinanceContext, ready: Sequence[StatementReady]
) -> None:
    today = local_today(ctx.clock, ctx.settings.timezone)
    periods: dict[uuid.UUID, set[dt.date]] = {}
    for row in uow.execute(
        select(statement_period.c.account_id, statement_period.c.period_end)
        .join(document, document.c.id == statement_period.c.document_id)
        .where(document.c.status == "parsed")
    ):
        periods.setdefault(row.account_id, set()).add(row.period_end)
    accounts = {row.id: row for row in uow.execute(select(account))}
    available: dict[tuple[uuid.UUID, dt.date], dt.date] = {}
    for fact in ready:
        if (
            fact.account_id in accounts
            and fact.ready_on <= today
            and fact.period_end not in periods.get(fact.account_id, set())
        ):
            key = (fact.account_id, fact.period_end)
            available[key] = min(available.get(key, fact.ready_on), fact.ready_on)
    for account_id, row in accounts.items():
        known = [day for day in periods.get(account_id, set()) if day <= today]
        if row.cadence == "none" or not known:
            continue
        last = max(known)
        expected = next_period(last, row.cadence)
        # No statement is expected after an account closes, or before it opens.
        if (row.closed_on is not None and expected > row.closed_on) or (
            row.opened_on is not None and expected < row.opened_on
        ):
            continue
        available_on = expected + dt.timedelta(days=ctx.settings.get_int(LAG_KEY))
        if today >= available_on and expected not in periods.get(account_id, set()):
            key = (account_id, expected)
            available[key] = min(available.get(key, available_on), available_on)
        overdue_on = expected + dt.timedelta(days=ctx.settings.get_int(GRACE_KEY))
        if today >= overdue_on:
            record_event(
                uow,
                EVENT_MODELS,
                STATEMENT_OVERDUE,
                f"finance.account:{account_id}",
                overdue_on,
                StatementOverdue(last_period_end=last),
                key=(last,),
                identity_on=last,
            )
    for (account_id, period), day in sorted(available.items()):
        record_event(
            uow,
            EVENT_MODELS,
            STATEMENT_AVAILABLE,
            f"finance.account:{account_id}",
            day,
            StatementAvailable(period_end=period),
            key=(period,),
            identity_on=period,
        )


def _spells(uow: UnitOfWork) -> None:
    """Keep only actual parser spells of current documents. Inferred split-line
    intermediate balances are deliberately never read here. Phase 5 adds the
    running-balance-source recompute and overdraft events on this derived seam."""
    found: dict[tuple[uuid.UUID, str, dt.date], dt.date | None] = {}
    rows = uow.execute(
        select(parsed_overdrawn)
        .join(document, document.c.id == parsed_overdrawn.c.document_id)
        .where(document.c.status == "parsed")
    )
    for row in rows:
        key = (row.account_id, row.currency, row.start_date)
        if key not in found:
            found[key] = row.end_date
        elif found[key] is None or row.end_date is None:
            found[key] = None
        else:
            found[key] = max(found[key], row.end_date)
    if found:
        uow.execute(
            insert(overdrawn_spell),
            [
                dict(
                    account_id=key[0], currency=key[1], start_date=key[2], end_date=end
                )
                for key, end in sorted(found.items())
            ],
        )


def build(
    uow: UnitOfWork, ctx: FinanceContext, *, statement_ready: ReadyLoader = load_ready
) -> None:
    """Record facts independently of sinks; retain delivery states and times."""
    today = local_today(ctx.clock, ctx.settings.timezone)
    now = ctx.clock.now_utc()
    _obligations(uow, today, now)
    _reversals(uow)
    _statements(uow, ctx, statement_ready(uow))
    _spells(uow)
    for row in uow.execute(
        select(event.c.id, event.c.occurred_on).where(event.c.type.in_(ALERT_TYPES))
    ):
        _notice(uow, f"alert:{row.id}", "alert", row.occurred_on, now)
