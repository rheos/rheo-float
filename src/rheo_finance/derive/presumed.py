"""Derivation stage 14's last word on an obligation: presumed paid, the reminders floor
and the presumed-paid candidates (FR-19, FR-26, FR-67; SPEC Derivation stage 14
"Presumed paid" and "The paying account of a payee"; edge case 80; AC-10, AC-82).

**Presumed paid.** An obligation named by an unreversed ``presumed_paid`` decision
(``finance.obligation:<id>``; the id comes from the natural key, so it survives a
rebuild) carries the newest such decision in ``presumed_paid_decision_id``, and an
``open`` one has status ``presumed_paid`` (``mark_presumed``, run each time stage 14
makes the obligations, before stage 15 applies). It then behaves as paid: no live
candidate for a payment, nothing overdue, no ticket or nudge. Stage 15's application
reverts it to a payment-backed status the moment a payment applies to it
(``stage15_apply``); the decision stays on record and changes nothing. A superseded or
already paid obligation keeps its status.

**The floor.** ``finance.reminders.min_due_date`` has no default. An obligation due
before it is ``before_floor`` (``mark_floor``): ``owed`` still shows it, marked "before
reminders floor", and it gets no ticket or nudge, whatever its status. Unset, nothing is
before the floor, and an obligation with no due date never is.

**Candidates.** ``presumed_paid_candidates`` lists the ``open`` obligations before the
floor with nothing paid, due in the current local month or earlier (presumed paid is a
statement about the past, edge case 24), whose payee's paying account has a gap for the
due month or the month after: the payment can sit there. A month is a gap only where
the account could have held the payment: its coverage is any state of stage 6 but
``have`` and ``not_applicable`` (``GAP_STATES``), or it has no coverage row and the
account has no known opened date. A month that is ``not_applicable``, or wholly before
the account's opened date or after its closed date, is never a gap, and neither is a
``have`` month. The paying accounts of a payee, in this precedence:

1. the ``paid_from`` accounts of its ``payees.csv`` row, when set, the whole set;
2. else the accounts whose lines are ``money`` evidence of an active payment to it
   (``payment_evidence`` after stage 15);
3. else every own account of kind ``chequing``.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Final

from sqlalchemy import bindparam, select, update

from rheo_finance.context import UnitOfWork
from rheo_finance.decision import DecisionKind, reversed_clause
from rheo_finance.derive.coverage import STATES, month_end, month_start
from rheo_finance.derive.stage15 import ACTIVE, MONEY
from rheo_finance.derive.stage15_apply import OBLIGATION_TYPE, OPEN, PRESUMED_PAID
from rheo_finance.storage import refs
from rheo_finance.storage.schema import (
    account,
    coverage,
    decision,
    obligation,
    payee,
    payment,
    payment_evidence,
    txn,
)

FLOOR_KEY: Final = "finance.reminders.min_due_date"
FLOOR_MARKER: Final = "before reminders floor"
CHEQUING: Final = "chequing"
# Coverage states a payment could hide in: every state stage 6 writes but a month it
# has and a month the account was not open, so a new state is a gap until said not.
GAP_STATES: Final = frozenset(STATES) - {"have", "not_applicable"}


def presumed(uow: UnitOfWork) -> dict[uuid.UUID, uuid.UUID]:
    """The newest unreversed ``presumed_paid`` decision per obligation id it names."""
    rows = uow.execute(
        select(decision.c.id, decision.c.obligation_ref)
        .where(
            decision.c.kind == DecisionKind.PRESUMED_PAID.value,
            decision.c.obligation_ref.is_not(None),
            ~reversed_clause(decision),
        )
        .order_by(decision.c.decided_at.desc(), decision.c.id.desc())
    ).all()
    found: dict[uuid.UUID, uuid.UUID] = {}
    for row in rows:
        ref = refs.parse(row.obligation_ref)
        if ref.system == refs.FINANCE_SYSTEM and ref.type == OBLIGATION_TYPE:
            found.setdefault(uuid.UUID(ref.id), row.id)
    return found


def mark_presumed(uow: UnitOfWork, named: Mapping[uuid.UUID, uuid.UUID]) -> int:
    """Name each obligation's decision and make the ``open`` ones ``presumed_paid``;
    how many it made so. A decision naming no current obligation changes nothing."""
    if not named:
        return 0
    values = [{"b_id": key, "b_decision": value} for key, value in named.items()]
    uow.execute(
        update(obligation)
        .where(obligation.c.id == bindparam("b_id"))
        .values(presumed_paid_decision_id=bindparam("b_decision")),
        values,
    )
    result = uow.execute(
        update(obligation)
        .where(obligation.c.id.in_(list(named)), obligation.c.status == OPEN)
        .values(status=PRESUMED_PAID)
    )
    return result.rowcount


def mark_floor(uow: UnitOfWork, floor: dt.date | None) -> int:
    """Mark the obligations due before ``floor``; how many."""
    if floor is None:
        return 0
    result = uow.execute(
        update(obligation)
        .where(obligation.c.due_date < floor)
        .values(before_floor=True)
    )
    return result.rowcount


@dataclass(frozen=True)
class Candidate:
    """An obligation the confirm queue may offer as presumed paid, with the paying
    accounts (by slug) whose coverage left room for its payment."""

    obligation_id: uuid.UUID
    payee_id: uuid.UUID
    kind: str
    period_label: str
    due_date: dt.date
    remaining: Decimal
    gaps: tuple[str, ...]


def _paying_accounts(uow: UnitOfWork) -> dict[uuid.UUID, frozenset[uuid.UUID]]:
    """Every payee's paying accounts, by the precedence of the module docstring."""
    chequing = frozenset(
        uow.scalars(select(account.c.id).where(account.c.kind == CHEQUING))
    )
    evidence: dict[uuid.UUID, set[uuid.UUID]] = defaultdict(set)
    for row in uow.execute(
        select(payment.c.payee_id, txn.c.account_id)
        .select_from(
            payment_evidence.join(
                payment, payment.c.id == payment_evidence.c.payment_id
            ).join(txn, txn.c.id == payment_evidence.c.txn_id)
        )
        .where(payment.c.state == ACTIVE, payment_evidence.c.role == MONEY)
    ):
        evidence[row.payee_id].add(row.account_id)
    found: dict[uuid.UUID, frozenset[uuid.UUID]] = {}
    for row in uow.execute(select(payee.c.id, payee.c.paid_from)):
        if row.paid_from:
            found[row.id] = frozenset(row.paid_from)
        elif row.id in evidence:
            found[row.id] = frozenset(evidence[row.id])
        else:
            found[row.id] = chequing
    return found


@dataclass(frozen=True)
class _Gaps:
    """Each account's coverage state per month, and its opened and closed dates."""

    states: Mapping[tuple[uuid.UUID, dt.date], str]
    open_dates: Mapping[uuid.UUID, tuple[dt.date | None, dt.date | None]]

    @classmethod
    def read(cls, uow: UnitOfWork) -> _Gaps:
        states = {
            (row.account_id, row.month): row.state
            for row in uow.execute(
                select(coverage.c.account_id, coverage.c.month, coverage.c.state)
            )
        }
        open_dates = {
            row.id: (row.opened_on, row.closed_on)
            for row in uow.execute(
                select(account.c.id, account.c.opened_on, account.c.closed_on)
            )
        }
        return cls(states, open_dates)

    def gap(self, account_id: uuid.UUID, month: dt.date) -> bool:
        """Whether the payment could sit in ``account_id`` in ``month``."""
        state = self.states.get((account_id, month))
        if state is not None:
            # A month wholly outside the open dates is ``not_applicable``.
            return state in GAP_STATES
        # Stage 6 writes a row for every month from the first known date, which is on
        # or before the opened date, to the latest coverage row (the derive's current
        # month, or a later dated record): up to that row, a missing row of an opened
        # account is outside its open dates. Past it there is no row yet, and an opened
        # account reads as no gap there. One with no opened date may still have closed
        # before the month.
        opened, closed = self.open_dates.get(account_id, (None, None))
        return opened is None and (closed is None or month <= closed)


def presumed_paid_candidates(uow: UnitOfWork, today: dt.date) -> list[Candidate]:
    """The obligations the confirm queue offers as presumed paid (module docstring),
    by due date, as of the local date ``today``; each with the paying accounts that
    have a gap for its due month or the month after."""
    o = obligation.c
    rows = uow.execute(
        select(o.id, o.payee_id, o.kind, o.period_label, o.due_date, o.remaining)
        .where(
            # ``open``: nothing paid, and not presumed paid already.
            o.status == OPEN,
            o.before_floor.is_(True),
            # Due this month or earlier: nothing is presumed paid ahead of time.
            o.due_date <= month_end(today),
        )
        .order_by(o.due_date, o.period_label, o.id)
    ).all()
    if not rows:
        return []
    paying = _paying_accounts(uow)
    slugs = {
        row.id: row.slug for row in uow.execute(select(account.c.id, account.c.slug))
    }
    coverage_of = _Gaps.read(uow)
    found = []
    for row in rows:
        first = month_start(row.due_date)
        months = (first, month_end(first) + dt.timedelta(days=1))
        gaps = sorted(
            slugs[each]
            for each in paying.get(row.payee_id, frozenset())
            if any(coverage_of.gap(each, month) for month in months)
        )
        if gaps:
            found.append(
                Candidate(
                    obligation_id=row.id,
                    payee_id=row.payee_id,
                    kind=row.kind,
                    period_label=row.period_label,
                    due_date=row.due_date,
                    remaining=row.remaining,
                    gaps=tuple(gaps),
                )
            )
    return found
