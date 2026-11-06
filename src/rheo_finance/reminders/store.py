"""Minimal owner-side reminder facts; no account, balance or private text columns."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol

from sqlalchemy import select

from rheo_finance.context import UnitOfWork
from rheo_finance.storage.schema import obligation, payee


@dataclass(frozen=True)
class ReminderObligation:
    payee_slug: str
    payee_label: str
    period: str
    amount: Decimal
    due_date: dt.date | None
    status: str
    before_floor: bool


class ReminderRepository(Protocol):
    def obligations(self) -> tuple[ReminderObligation, ...]: ...

    def registered_payees(self) -> frozenset[str]: ...


class PostgresReminderRepository:
    def __init__(self, uow: UnitOfWork) -> None:
        self.uow = uow

    def registered_payees(self) -> frozenset[str]:
        return frozenset(self.uow.scalars(select(payee.c.display_label)))

    def obligations(self) -> tuple[ReminderObligation, ...]:
        o = obligation.c
        rows = self.uow.execute(
            select(
                payee.c.slug,
                payee.c.display_label,
                o.period_label,
                o.threshold_remaining,
                o.due_date,
                o.status,
                o.before_floor,
            )
            .select_from(obligation.join(payee, o.payee_id == payee.c.id))
            .order_by(payee.c.slug, o.period_label, o.kind)
        )
        return tuple(
            ReminderObligation(
                payee_slug=row.slug,
                payee_label=row.display_label,
                period=row.period_label,
                amount=row.threshold_remaining,
                due_date=row.due_date,
                status=row.status,
                before_floor=row.before_floor,
            )
            for row in rows
        )
