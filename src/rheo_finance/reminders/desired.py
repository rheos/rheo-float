"""Desired reminders for reconciliation, with no sink reads or writes."""

from __future__ import annotations

import datetime as dt
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict

from rheo_finance.clock import local_date
from rheo_finance.context import FinanceContext, UnitOfWork
from rheo_finance.contracts import Role, SafetyClass, Tiered
from rheo_finance.operations import OperationError, OperationSpec, register
from rheo_finance.reminders.policy import (
    BillPayload,
    PayloadPolicy,
    PayloadRefused,
    build_payload,
    safe_token,
)
from rheo_finance.reminders.store import PostgresReminderRepository, ReminderRepository
from rheo_finance.tier import Tier

P = Tier.PUBLIC


class DesiredIn(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class DesiredTicket(BillPayload):
    source_ref: Annotated[str, Tiered(P)]
    status: Annotated[Literal["open", "done"], Tiered(P)]
    wake: Annotated[dt.datetime | None, Tiered(P)]
    ministry: Annotated[Literal["plenty"], Tiered(P)] = "plenty"
    ticket_type: Annotated[Literal["finance-bill"], Tiered(P)] = "finance-bill"
    severity: Annotated[Literal["normal", "high"], Tiered(P)]
    provenance: Annotated[Literal["status-poll"], Tiered(P)] = "status-poll"


class DesiredOut(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    tickets: Annotated[list[DesiredTicket], Tiered(P)]


def desired_tickets(ctx: FinanceContext, repository: ReminderRepository) -> DesiredOut:
    floor = ctx.settings.get_date("finance.reminders.min_due_date")
    if floor is None:
        raise OperationError("set finance.reminders.min_due_date first")
    now = ctx.clock.now_utc()
    today = local_date(now, ctx.settings.timezone)
    registered = repository.registered_payees()
    tickets = []
    for row in repository.obligations():
        if (
            row.due_date is None
            or row.due_date < floor
            or row.before_floor
            or row.status in {"presumed_paid", "disputed"}
        ):
            continue
        if not safe_token(row.payee_slug):
            raise PayloadRefused("reminder payee key must be a safe token")
        payload = build_payload(
            PayloadPolicy(
                payee_label=row.payee_label,
                amount=row.amount,
                due_date=row.due_date,
                period=row.period,
            ),
            registered_payees=registered,
        )
        done = row.amount == 0 or row.status in {"paid", "superseded"}
        wake = dt.datetime.combine(
            row.due_date
            - dt.timedelta(
                days=ctx.settings.get_int("finance.reminders.wake_days_before")
            ),
            ctx.settings.get_time("finance.reminders.wake_time"),
            ctx.settings.timezone,
        ).astimezone(dt.UTC)
        tickets.append(
            DesiredTicket(
                **payload.model_dump(),
                source_ref=f"finance:{row.payee_slug}:{row.period}",
                status="done" if done else "open",
                wake=None if wake < now else wake,
                severity="high" if not done and row.due_date < today else "normal",
            )
        )
    return DesiredOut(tickets=tickets)


def desired(ctx: FinanceContext, uow: UnitOfWork, _data: DesiredIn) -> DesiredOut:
    return desired_tickets(ctx, PostgresReminderRepository(uow))


register(
    OperationSpec(
        name="finance.reminder.desired",
        input_model=DesiredIn,
        output_model=DesiredOut,
        handler=desired,
        safety_class=SafetyClass.READ,
        roles=frozenset({Role.OWNER, Role.SERVICE}),
        connection="owner",
        description="Desired reminder periods under the restricted payload policy.",
    )
)
