"""Derivation stage 15, application: what each payment paid (FR-21, FR-22; Key decisions
18 and 20; SPEC Derivation stage 15 "Application"; edge cases 14 to 17, 34 and 66).
Stage 14 made the obligations and ``stage15`` the payments; this reads both and writes
``payment_application``, then each obligation's ``paid``, ``remaining``,
``threshold_remaining``, status and ``payments_mismatch``.

Only an ``active`` payment applies: a ``reversed`` one applies nothing, so whatever it
had paid reopens, and a ``confirm_only`` one waits for the owner. Each active payment's
rows add up to its amount, in its currency: what it paid, and the rest as an unapplied
credit (a row with no obligation) with its reason.

**Order.** Payments go in ``paid_on`` order (then id), the owner's decisions first, then
the rules, so a decided payment has taken its share before a rule reads a remaining.

**Decisions.** ``payment_apply`` and ``payment_unapply`` name a payment by any line or
notice it was seen in (``finance.txn:<id>``, ``finance.payment_notice:<key>``). Newest
first per obligation: a ``payment_apply`` places the payment on the obligation it names,
for its ``amount`` when it gives one, else up to that obligation's remaining, whatever
the bound or the window; a ``payment_unapply`` naming an obligation bars it; one naming
none bars every older decision of that payment. A payment with an apply in force takes
nothing from the rules, and what is left over is ``excess``. One with only an unapply in
force applies nothing: the whole of it is a credit of reason ``payment_unapply``, so
undoing an application restores the remaining it had before. A decision naming an
obligation that does not exist, or a payment that is not active, changes nothing, and
neither does a ``payment_apply`` on a payment in another currency (every obligation is
in CAD); its ``payment_unapply`` still takes it off the rules.

**Cards, with no early_days slack.** A payment to a card's payee belongs to the card's
statement whose ``statement_date`` is the latest on or before ``paid_on``, whatever its
status (``disputed`` aside): a minimum paid on statement N's due date stays on N when
N+1 loads, because N+1's balance already nets it. Paid toward it, the payment fills the
threshold first, simply because ``threshold_remaining`` is the threshold less what was
paid. More than the remaining is ``excess``, never carried onto the next statement; less
is a partial payment, which applies when ``card_statement`` is in
``finance.payments.partial_auto_kinds`` and is a ``confirm`` credit otherwise. A card
payment dated before the card's earliest statement is ``no_target``; one in a currency
other than CAD is a ``confirm`` credit when its statement has something left past
the tolerance (below), and ``no_target`` when not. A superseded statement keeps what
was applied to it and lends nothing to the newer one.

**Every other kind.** An obligation is a candidate when its ``issued_on`` is on or
before ``paid_on`` plus ``finance.payments.early_days`` (a null ``issued_on`` is no
bound), and live when it is also ``open`` or ``partial`` as the payments before this one
left it. Among the live candidates:

- the ones whose remaining is the amount within ``match_tolerance`` (one live candidate
  or several, edge case 15): it pays the oldest of them, unless the oldest two were
  issued and are due on the same days, when nothing says which and it is ``confirm``;
- else, when every live candidate's kind is in the partial set (an arrangement's
  instalments by default), it fills them oldest first and the rest is ``excess``;
- else it is a ``confirm`` credit: two candidates and neither fits.

Oldest is by ``issued_on``, then due date, then the period label in its natural order
(``inst-plan-2`` before ``inst-plan-10``). With no live candidate, an obligation
the stage left ``superseded`` (or presumed paid) whose own window holds ``paid_on`` is
the target when the amount fits its ``amount_due`` or its kind is in the partial set:
the window runs from its ``issued_on`` less ``early_days`` up to, not including, the
next ``issued_on`` of the payee's same kind plus ``early_days`` (open for the newest;
one ``issued_on`` shared is one window). Two such targets, the amount picks one, else
``confirm``; one the amount does not fit, ``confirm``. With none (every candidate paid
already, or out of its window, or no candidate at all) it is ``no_target``, with
``bound_on`` the latest issue date a target could have had; ``excess`` is only the part
of a payment above what its target had left. So a credit never rolls forward onto an
obligation issued after it: it leaves that state by a decision or by a target that
passes the bound loading later. Only a CAD payment pays an obligation, since every
obligation is in CAD: another currency's payment is a ``confirm`` credit when a live
candidate has something to pay, and ``no_target`` when none has.

**Tolerance.** A payment within ``match_tolerance`` of what is left settles it: a
remaining (or threshold remaining) at or under the tolerance once something was paid is
written as zero, so a cent short leaves no overdue cent behind.

**Status.** ``open``, ``partial`` or ``paid`` from what was paid. A status stage 14 set
otherwise (``superseded``, and later ``disputed`` and ``presumed_paid``) stays: a
superseded row still shows what was applied to it.

**FR-21.** For each obligation that supersedes another and whose document prints a
payments figure (a card statement's ``total_payments``), the payments applied to its
payee dated after the older one's date and on or before its own are compared with that
figure; applied less printed is ``payments_mismatch`` ("applied X, statement shows Y"),
null when they agree.

**Arrangements.** ``finished_arrangements`` reads, after an application, the
arrangements whose every instalment is paid, each with the day the last one was paid;
``derive.run`` hands them back to stage 14, whose arrangement rule then no longer
governs a bill issued after that day plus ``early_days``, and applies again. A payment
that finished an arrangement is dated on or before that day, so its bound never
reaches a bill that finishing ungoverns, and the rules alone settle in one round; a
``payment_apply`` naming an arrears row that exists only once the arrangement is
finished moves the payment off it, and then no finished set holds. ``settled`` then
keeps the arrangements every round finished on one day (the rest stay active, their
bills governed, so nothing is owed twice) and ``mark_unsettled`` flags the instalments
of the rest, which status line 9 names (``arrangement_unsettled``), each instalment
with the decision that moved its payment off it (``unsettled_decision_id``).

**Events.** Each automatic application to an obligation registers one
``finance.payment.detected`` (``record_detected``): the payment, its payee and the
obligation, never an amount.
"""

from __future__ import annotations

import datetime as dt
import re
import uuid
from collections import defaultdict
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from types import MappingProxyType
from typing import Annotated, Final, NamedTuple

from pydantic import BaseModel, ConfigDict
from sqlalchemy import bindparam, func, insert, select, update

from rheo_finance.context import UnitOfWork
from rheo_finance.contracts import Tiered
from rheo_finance.decision import DecisionKind, reversed_clause
from rheo_finance.derive.stage4 import cents
from rheo_finance.derive.stage14 import CARD_STATEMENT, CURRENCY, INSTALMENT
from rheo_finance.derive.stage15 import ACTIVE, NOTICE_KIND
from rheo_finance.events import record_event
from rheo_finance.manual.accounts import CREDIT_CARD
from rheo_finance.storage import refs
from rheo_finance.storage.ids import derived_id
from rheo_finance.storage.schema import (
    account,
    decision,
    document,
    document_line,
    obligation,
    payee,
    payment,
    payment_application,
    payment_evidence,
    statement_period,
)
from rheo_finance.tier import Tier

EARLY_DAYS_KEY: Final = "finance.payments.early_days"
PARTIAL_KINDS_KEY: Final = "finance.payments.partial_auto_kinds"

OPEN: Final = "open"
PARTIAL: Final = "partial"
PAID: Final = "paid"
DISPUTED: Final = "disputed"
SUPERSEDED: Final = "superseded"
PRESUMED_PAID: Final = "presumed_paid"
# Statuses stage 14 sets that a payment does not change.
KEPT: Final = frozenset({SUPERSEDED, DISPUTED, PRESUMED_PAID})
# Statuses whose own window can still take a payment, with no live candidate.
WINDOWED: Final = frozenset({SUPERSEDED, PRESUMED_PAID})

NO_TARGET: Final = "no_target"
EXCESS: Final = "excess"
CONFIRM: Final = "confirm"
UNAPPLIED: Final = DecisionKind.PAYMENT_UNAPPLY.value

OBLIGATION_TYPE: Final = "obligation"
PAYMENT_TYPE: Final = "payment"
PAYEE_TYPE: Final = "payee"
DECISION_TYPE: Final = "decision"
ZERO: Final = Decimal("0.00")

# --- events ---------------------------------------------------------------------------

PAYMENT_DETECTED: Final = "finance.payment.detected"


class PaymentDetected(BaseModel):
    """A payment a rule applied to an obligation: refs only, never an amount."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    payee_ref: Annotated[str, Tiered(Tier.PUBLIC)]
    obligation_ref: Annotated[str, Tiered(Tier.PUBLIC)]


EVENT_MODELS: Final[Mapping[str, type[BaseModel]]] = MappingProxyType(
    {PAYMENT_DETECTED: PaymentDetected}
)


@dataclass(frozen=True)
class Rules:
    """The settings application reads."""

    tolerance: Decimal
    early_days: int
    partial_kinds: frozenset[str]


@dataclass
class _Obligation:
    id: uuid.UUID
    payee_id: uuid.UUID
    kind: str
    period_label: str
    statement_date: dt.date | None
    issued_on: dt.date | None
    due_date: dt.date | None
    amount_due: Decimal
    threshold_due: Decimal
    # As stage 14 set it.
    base_status: str
    supersedes_id: uuid.UUID | None
    arrangement_ref: str | None
    primary_source: str
    paid: Decimal = ZERO

    @property
    def remaining(self) -> Decimal:
        return max(self.amount_due - self.paid, ZERO)

    def owes(self, tolerance: Decimal) -> bool:
        """Whether something is left once ``tolerance`` settles what was paid."""
        return not (self.remaining == 0 or (self.paid and self.remaining <= tolerance))

    def status(self, tolerance: Decimal) -> str:
        if self.base_status in KEPT:
            return self.base_status
        if not self.owes(tolerance):
            return PAID
        return PARTIAL if self.paid else OPEN

    @property
    def issue(self) -> tuple[dt.date, dt.date]:
        """When it was issued and is due: two that share it are the same age."""
        return (self.issued_on or dt.date.min, self.due_date or dt.date.min)

    @property
    def age(self) -> tuple[dt.date, dt.date, tuple[tuple[int, int, str], ...]]:
        return (*self.issue, _natural(self.period_label))


def _natural(label: str) -> tuple[tuple[int, int, str], ...]:
    """A period label in its natural order: a run of digits by its number, so
    ``inst-plan-2`` comes before ``inst-plan-10``."""
    return tuple(
        (0, int(part), "") if part.isdigit() else (1, 0, part)
        for part in re.split(r"(\d+)", label)
        if part
    )


@dataclass(frozen=True)
class _Payment:
    id: uuid.UUID
    payee_id: uuid.UUID
    amount: Decimal
    currency: str
    paid_on: dt.date


@dataclass
class _Rows:
    """The applications being written, one per (payment, obligation or reason)."""

    rows: dict[tuple[uuid.UUID, object], dict[str, object]] = field(
        default_factory=dict
    )

    def add(
        self,
        paid: _Payment,
        amount: Decimal,
        *,
        target: _Obligation | None = None,
        reason: str | None = None,
        bound_on: dt.date | None = None,
        basis_ref: str | None = None,
        auto: bool = True,
    ) -> None:
        if amount <= 0:
            return
        if target is not None:
            target.paid += amount
        subject: object = reason if target is None else target.id
        key = (paid.id, subject)
        found = self.rows.get(key)
        if found is not None:
            found["amount"] = Decimal(str(found["amount"])) + amount
            return
        self.rows[key] = {
            "id": derived_id(paid.paid_on, ("payment_application", paid.id, subject)),
            "payment_id": paid.id,
            "obligation_id": None if target is None else target.id,
            "amount": amount,
            "reason": reason,
            "bound_on": bound_on,
            "basis_ref": basis_ref,
            "auto": auto,
        }


class _DecisionRow(NamedTuple):
    id: uuid.UUID
    kind: str
    target_ref: str
    obligation_ref: str | None
    amount: Decimal | None


@dataclass(frozen=True)
class _Decided:
    """The decisions in force on one payment."""

    # (obligation id, amount or None, the decision's ref), newest first.
    applies: tuple[tuple[uuid.UUID, Decimal | None, str], ...]
    # The newest unapply in force, when no apply is.
    unapplied: str | None


def _obligations(uow: UnitOfWork) -> dict[uuid.UUID, _Obligation]:
    o = obligation.c
    rows = uow.execute(
        select(
            o.id,
            o.payee_id,
            o.kind,
            o.period_label,
            o.statement_date,
            o.issued_on,
            o.due_date,
            o.amount_due,
            o.threshold_due,
            o.status,
            o.supersedes_id,
            o.arrangement_ref,
            o.primary_source,
        )  # fmt: skip
    ).all()
    return {
        row.id: _Obligation(
            id=row.id,
            payee_id=row.payee_id,
            kind=row.kind,
            period_label=row.period_label,
            statement_date=row.statement_date,
            issued_on=row.issued_on,
            due_date=row.due_date,
            amount_due=row.amount_due,
            threshold_due=row.threshold_due,
            base_status=row.status,
            supersedes_id=row.supersedes_id,
            arrangement_ref=row.arrangement_ref,
            primary_source=row.primary_source,
        )
        for row in rows
    }


def _payments(uow: UnitOfWork) -> list[_Payment]:
    """The active payments, in ``paid_on`` order, then id."""
    rows = uow.execute(
        select(
            payment.c.id,
            payment.c.payee_id,
            payment.c.amount,
            payment.c.currency,
            payment.c.paid_on,
        )
        .where(payment.c.state == ACTIVE)
        .order_by(payment.c.paid_on, payment.c.id)
    ).all()
    return [
        _Payment(row.id, row.payee_id, row.amount, row.currency, row.paid_on)
        for row in rows
    ]


def _card_payees(uow: UnitOfWork) -> frozenset[uuid.UUID]:
    return frozenset(
        uow.scalars(
            select(payee.c.id)
            .join(account, payee.c.account_id == account.c.id)
            .where(account.c.kind == CREDIT_CARD)
        )
    )


def _payment_of_ref(uow: UnitOfWork, named: Iterable[str]) -> dict[str, uuid.UUID]:
    """The payment each ref names through a line or a notice it was seen in."""
    lines: dict[uuid.UUID, str] = {}
    notices: dict[uuid.UUID, str] = {}
    for text in named:
        ref = refs.parse(text)
        if ref.system != refs.FINANCE_SYSTEM:
            continue
        if ref.type == refs.LINE_TYPE:
            lines[uuid.UUID(ref.id)] = text
        elif ref.type == NOTICE_KIND:
            notices[uuid.UUID(ref.id)] = text
    found: dict[str, uuid.UUID] = {}
    if lines:
        for row in uow.execute(
            select(payment_evidence.c.txn_id, payment_evidence.c.payment_id).where(
                payment_evidence.c.txn_id.in_(lines)
            )
        ):
            found[lines[row.txn_id]] = row.payment_id
    if notices:
        for row in uow.execute(
            select(document_line.c.line_key_id, payment_evidence.c.payment_id)
            .join(
                payment_evidence,
                payment_evidence.c.document_line_id == document_line.c.id,
            )
            .where(document_line.c.line_key_id.in_(notices))
        ):
            found[notices[row.line_key_id]] = row.payment_id
    return found


def _obligation_of_ref(text: str | None) -> uuid.UUID | None:
    if text is None:
        return None
    ref = refs.parse(text)
    if ref.system != refs.FINANCE_SYSTEM or ref.type != OBLIGATION_TYPE:
        return None
    return uuid.UUID(ref.id)


def _decisions(
    uow: UnitOfWork, known: Collection[uuid.UUID]
) -> dict[uuid.UUID, _Decided]:
    """The ``payment_apply`` and ``payment_unapply`` decisions in force, per payment
    (module docstring)."""
    rows = uow.execute(
        select(
            decision.c.id,
            decision.c.kind,
            decision.c.target_ref,
            decision.c.obligation_ref,
            decision.c.amount,
        )
        .where(
            decision.c.kind.in_(
                [DecisionKind.PAYMENT_APPLY.value, DecisionKind.PAYMENT_UNAPPLY.value]
            ),
            ~reversed_clause(decision),
        )
        .order_by(decision.c.decided_at.desc(), decision.c.id.desc())
    ).all()
    if not rows:
        return {}
    of_ref = _payment_of_ref(uow, {row.target_ref for row in rows})
    by_payment: dict[uuid.UUID, list[_DecisionRow]] = defaultdict(list)
    for row in rows:
        paid = of_ref.get(row.target_ref)
        if paid is not None:
            by_payment[paid].append(_DecisionRow(*row))
    decided: dict[uuid.UUID, _Decided] = {}
    for paid, newest_first in by_payment.items():
        settled: set[uuid.UUID] = set()
        applies: list[tuple[uuid.UUID, Decimal | None, str]] = []
        unapplied: str | None = None
        for each in newest_first:
            basis = refs.format_ref(refs.FINANCE_SYSTEM, DECISION_TYPE, each.id)
            named = _obligation_of_ref(each.obligation_ref)
            if each.kind == DecisionKind.PAYMENT_UNAPPLY.value:
                unapplied = unapplied or basis
                if named is None:
                    break
                settled.add(named)
            elif named is not None and named not in settled and named in known:
                settled.add(named)
                applies.append((named, each.amount, basis))
        if applies or unapplied is not None:
            decided[paid] = _Decided(tuple(applies), None if applies else unapplied)
    return decided


class _Applier:
    """The running state of one application: every obligation as the payments so far
    left it, and the rows."""

    def __init__(
        self, obligations: Mapping[uuid.UUID, _Obligation], cards: Collection[uuid.UUID]
    ) -> None:
        self.obligations = obligations
        self.cards = cards
        self.rows = _Rows()
        self.by_payee: dict[uuid.UUID, list[_Obligation]] = defaultdict(list)
        for each in sorted(obligations.values(), key=lambda x: x.age):
            self.by_payee[each.payee_id].append(each)

    # --- decisions --------------------------------------------------------------------

    def decided(self, paid: _Payment, found: _Decided) -> None:
        left = paid.amount
        for obligation_id, amount, basis in reversed(found.applies):
            target = self.obligations[obligation_id]
            share = min(left, target.remaining if amount is None else amount)
            self.rows.add(paid, share, target=target, basis_ref=basis, auto=False)
            left -= max(share, ZERO)
        if found.unapplied is not None:
            self.rows.add(
                paid, left, reason=UNAPPLIED, basis_ref=found.unapplied, auto=False
            )
        else:
            self.rows.add(paid, left, reason=EXCESS, auto=False)

    # --- the rules --------------------------------------------------------------------

    def auto(self, paid: _Payment, rules: Rules) -> None:
        if paid.payee_id in self.cards:
            self._card(paid, rules)
        else:
            self._other(paid, rules)

    def _fill(
        self, paid: _Payment, target: _Obligation, rules: Rules, *, partial: bool
    ) -> None:
        """Pay ``target`` as much as it has left; more is ``excess``, less is a partial
        payment when ``partial`` allows one and a ``confirm`` credit when not."""
        left = target.remaining
        if (
            paid.amount <= left + rules.tolerance
            and paid.amount >= left - rules.tolerance
        ):
            self.rows.add(paid, paid.amount, target=target)
        elif paid.amount > left:
            self.rows.add(paid, left, target=target)
            self.rows.add(paid, paid.amount - left, reason=EXCESS)
        elif partial:
            self.rows.add(paid, paid.amount, target=target)
        else:
            self.rows.add(paid, paid.amount, reason=CONFIRM)

    def _foreign(self, paid: _Payment, *, owing: bool, bound: dt.date) -> None:
        """A payment in another currency: a ``confirm`` credit when its target is
        ``owing`` something, else ``no_target``."""
        if owing:
            self.rows.add(paid, paid.amount, reason=CONFIRM)
        else:
            self.rows.add(paid, paid.amount, reason=NO_TARGET, bound_on=bound)

    def _card(self, paid: _Payment, rules: Rules) -> None:
        statements = [
            each
            for each in self.by_payee[paid.payee_id]
            if each.kind == CARD_STATEMENT
            and each.base_status != DISPUTED
            and each.statement_date is not None
            and each.statement_date <= paid.paid_on
        ]
        if not statements:
            self.rows.add(paid, paid.amount, reason=NO_TARGET, bound_on=paid.paid_on)
            return
        target = max(statements, key=lambda each: (each.statement_date, each.age))
        if paid.currency != CURRENCY:
            self._foreign(paid, owing=target.owes(rules.tolerance), bound=paid.paid_on)
            return
        self._fill(paid, target, rules, partial=CARD_STATEMENT in rules.partial_kinds)

    def _other(self, paid: _Payment, rules: Rules) -> None:
        bound = paid.paid_on + dt.timedelta(days=rules.early_days)
        mine = [
            each for each in self.by_payee[paid.payee_id] if each.kind != CARD_STATEMENT
        ]
        candidates = [
            each for each in mine if each.issued_on is None or each.issued_on <= bound
        ]
        if not candidates:
            self.rows.add(paid, paid.amount, reason=NO_TARGET, bound_on=bound)
            return
        live = [
            each
            for each in candidates
            if each.status(rules.tolerance) in (OPEN, PARTIAL)
        ]
        if paid.currency != CURRENCY:
            self._foreign(paid, owing=bool(live), bound=bound)
            return
        if live:
            self._live(paid, live, rules)
            return
        windowed = [
            each
            for each in candidates
            if each.base_status in WINDOWED and self._in_window(each, mine, paid, rules)
        ]
        if windowed:
            self._windowed(paid, windowed, rules)
        else:
            self.rows.add(paid, paid.amount, reason=NO_TARGET, bound_on=bound)

    def _live(self, paid: _Payment, live: Sequence[_Obligation], rules: Rules) -> None:
        fits = [
            each
            for each in live
            if abs(each.remaining - paid.amount) <= rules.tolerance
        ]
        if len(fits) > 1 and fits[0].issue == fits[1].issue:
            # Two that fit, issued and due on the same days: nothing says which.
            self.rows.add(paid, paid.amount, reason=CONFIRM)
        elif fits:
            self.rows.add(paid, paid.amount, target=fits[0])
        elif all(each.kind in rules.partial_kinds for each in live):
            left = paid.amount
            for each in live:
                share = min(left, each.remaining)
                self.rows.add(paid, share, target=each)
                left -= share
            self.rows.add(paid, left, reason=EXCESS)
        else:
            self.rows.add(paid, paid.amount, reason=CONFIRM)

    @staticmethod
    def _in_window(
        target: _Obligation,
        mine: Sequence[_Obligation],
        paid: _Payment,
        rules: Rules,
    ) -> bool:
        """Whether ``paid`` is dated before the end of ``target``'s own window. The
        start, ``issued_on`` less ``early_days``, is the bound candidates passed."""
        if target.issued_on is None:
            return False
        early = dt.timedelta(days=rules.early_days)
        later = [
            each.issued_on
            for each in mine
            if each.kind == target.kind
            and each.issued_on is not None
            and each.issued_on > target.issued_on
        ]
        return not later or paid.paid_on < min(later) + early

    def _windowed(
        self, paid: _Payment, windowed: Sequence[_Obligation], rules: Rules
    ) -> None:
        exact = [
            each
            for each in windowed
            if abs(each.amount_due - paid.amount) <= rules.tolerance
        ]
        fits = exact or [each for each in windowed if each.kind in rules.partial_kinds]
        if len(fits) == 1:
            target = fits[0]
            self._fill(paid, target, rules, partial=target.kind in rules.partial_kinds)
        else:
            self.rows.add(paid, paid.amount, reason=CONFIRM)


def _printed_payments(uow: UnitOfWork) -> dict[str, Decimal]:
    """The payments figure each parsed document prints, by its archive path."""
    rows = uow.execute(
        select(document.c.path, statement_period.c.total_payments)
        .join(statement_period, statement_period.c.document_id == document.c.id)
        .where(
            document.c.status == "parsed",
            statement_period.c.currency == CURRENCY,
            statement_period.c.total_payments.is_not(None),
        )
    ).all()
    return {row.path: row.total_payments for row in rows}


def _mismatches(
    uow: UnitOfWork,
    obligations: Mapping[uuid.UUID, _Obligation],
    payments: Sequence[_Payment],
    rows: Iterable[Mapping[str, object]],
) -> dict[uuid.UUID, Decimal]:
    """FR-21: applied less printed, per newer document that prints a payments figure."""
    printed = _printed_payments(uow)
    applied_of: dict[uuid.UUID, Decimal] = defaultdict(lambda: ZERO)
    for row in rows:
        if row["reason"] in (None, EXCESS):
            amount = row["amount"]
            assert isinstance(amount, Decimal)
            payment_id = row["payment_id"]
            assert isinstance(payment_id, uuid.UUID)
            applied_of[payment_id] += amount
    found: dict[uuid.UUID, Decimal] = {}

    def dated(each: _Obligation) -> dt.date | None:
        return each.statement_date or each.issued_on

    for each in obligations.values():
        figure = printed.get(each.primary_source)
        older = (
            None if each.supersedes_id is None else obligations.get(each.supersedes_id)
        )
        start, end = (None, None) if older is None else (dated(older), dated(each))
        if figure is None or start is None or end is None:
            continue
        applied = sum(
            (
                applied_of[paid.id]
                for paid in payments
                if paid.payee_id == each.payee_id and start < paid.paid_on <= end
            ),
            ZERO,
        )
        if cents(applied) != cents(figure):
            found[each.id] = cents(applied - figure)
    return found


def apply_payments(uow: UnitOfWork, rules: Rules) -> int:
    """Write every active payment's applications and each obligation's figures
    (module docstring); how many application rows it wrote. The caller cleared
    ``payment_application`` and made the obligations afresh."""
    obligations = _obligations(uow)
    payments = _payments(uow)
    decided = _decisions(uow, obligations.keys())
    applier = _Applier(obligations, _card_payees(uow))
    # Only a CAD payment pays an obligation: an apply on another currency's payment
    # changes nothing, while its unapply still takes it off the rules.
    decided = {
        each.id: found
        for each in payments
        if (found := decided.get(each.id)) is not None
        and (each.currency == CURRENCY or not found.applies)
    }
    for paid in payments:
        found = decided.get(paid.id)
        if found is not None:
            applier.decided(paid, found)
    for paid in payments:
        if paid.id not in decided:
            applier.auto(paid, rules)
    rows = list(applier.rows.rows.values())
    if rows:
        uow.execute(insert(payment_application), rows)
    mismatch = _mismatches(uow, obligations, payments, rows)

    def left(due: Decimal, paid: Decimal) -> Decimal:
        """What is left of ``due``; a tolerance's worth once something was paid is
        nothing."""
        rest = max(due - paid, ZERO)
        return ZERO if paid and rest <= rules.tolerance else cents(rest)

    figures = [
        {
            "b_id": each.id,
            "b_paid": cents(each.paid),
            "b_remaining": left(each.amount_due, each.paid),
            "b_threshold": left(each.threshold_due, each.paid),
            "b_status": each.status(rules.tolerance),
            "b_mismatch": mismatch.get(each.id),
        }
        for each in obligations.values()
    ]
    if figures:
        uow.execute(
            update(obligation)
            .where(obligation.c.id == bindparam("b_id"))
            .values(
                paid=bindparam("b_paid"),
                remaining=bindparam("b_remaining"),
                threshold_remaining=bindparam("b_threshold"),
                status=bindparam("b_status"),
                payments_mismatch=bindparam("b_mismatch"),
            ),
            figures,
        )
    return len(rows)


def finished_arrangements(uow: UnitOfWork) -> dict[str, dt.date]:
    """The arrangements whose every instalment is paid, as the last application left
    them, each with the latest date of a payment applied to its instalments: the day
    its last instalment was paid."""
    rows = uow.execute(
        select(
            obligation.c.arrangement_ref,
            obligation.c.status,
            func.max(payment.c.paid_on).label("last_paid"),
        )
        .select_from(
            obligation.outerjoin(
                payment_application,
                payment_application.c.obligation_id == obligation.c.id,
            ).outerjoin(payment, payment.c.id == payment_application.c.payment_id)
        )
        .where(obligation.c.kind == INSTALMENT)
        .group_by(obligation.c.id, obligation.c.arrangement_ref, obligation.c.status)
    ).all()
    statuses: dict[str, set[str]] = defaultdict(set)
    last: dict[str, dt.date] = {}
    for row in rows:
        statuses[row.arrangement_ref].add(row.status)
        if row.last_paid is not None:
            last[row.arrangement_ref] = max(
                last.get(row.arrangement_ref, row.last_paid), row.last_paid
            )
    return {
        slug: last[slug]
        for slug, found in statuses.items()
        if found == {PAID} and slug in last
    }


def settled(rounds: Sequence[Mapping[str, dt.date]]) -> dict[str, dt.date]:
    """The conservative reading of finished sets that would not settle: the
    arrangements every round of ``rounds`` finished, on one day. One left out stays
    active, so its bills stay governed and nothing is owed twice."""
    first, *rest = rounds
    return {
        slug: day
        for slug, day in first.items()
        if all(each.get(slug) == day for each in rest)
    }


def _unsettling(uow: UnitOfWork, slugs: Collection[str]) -> dict[uuid.UUID, uuid.UUID]:
    """Per instalment of the arrangements named, the newest unreversed
    ``payment_apply`` decision on the payment that paid it whose obligation is not
    there now: with the arrangement kept active, the row that decision names (an
    arrears row of the finished arrangement) is not made, so the rules paid the
    instalment instead, and that decision is what would not settle."""
    paid_by: dict[uuid.UUID, list[uuid.UUID]] = defaultdict(list)
    for row in uow.execute(
        select(payment_application.c.payment_id, obligation.c.id)
        .join(obligation, obligation.c.id == payment_application.c.obligation_id)
        .where(
            obligation.c.kind == INSTALMENT,
            obligation.c.arrangement_ref.in_(list(slugs)),
        )
    ):
        paid_by[row.payment_id].append(row.id)
    if not paid_by:
        return {}
    rows = uow.execute(
        select(decision.c.id, decision.c.target_ref, decision.c.obligation_ref)
        .where(
            decision.c.kind == DecisionKind.PAYMENT_APPLY.value,
            decision.c.obligation_ref.is_not(None),
            ~reversed_clause(decision),
        )
        .order_by(decision.c.decided_at.desc(), decision.c.id.desc())
    ).all()
    of_ref = _payment_of_ref(uow, {row.target_ref for row in rows})
    present = set(uow.scalars(select(obligation.c.id)))
    found: dict[uuid.UUID, uuid.UUID] = {}
    for row in rows:
        named = _obligation_of_ref(row.obligation_ref)
        paid = of_ref.get(row.target_ref)
        if named is None or named in present or paid is None:
            continue
        for instalment in paid_by.get(paid, ()):
            found.setdefault(instalment, row.id)
    return found


def mark_unsettled(uow: UnitOfWork, slugs: Collection[str]) -> int:
    """Flag the instalments of the arrangements named, for status line 9, each with
    the decision that would not let it settle when one is found; how many."""
    if not slugs:
        return 0
    result = uow.execute(
        update(obligation)
        .where(
            obligation.c.kind == INSTALMENT,
            obligation.c.arrangement_ref.in_(list(slugs)),
        )
        .values(arrangement_unsettled=True)
    )
    named = _unsettling(uow, slugs)
    if named:
        uow.execute(
            update(obligation)
            .where(obligation.c.id == bindparam("b_id"))
            .values(unsettled_decision_id=bindparam("b_decision")),
            [{"b_id": key, "b_decision": value} for key, value in named.items()],
        )
    return result.rowcount


def record_detected(uow: UnitOfWork) -> int:
    """One ``finance.payment.detected`` per automatic application to an obligation;
    how many."""
    rows = uow.execute(
        select(
            payment_application.c.payment_id,
            payment_application.c.obligation_id,
            payment.c.payee_id,
            payment.c.paid_on,
        )
        .join(payment, payment.c.id == payment_application.c.payment_id)
        .where(
            payment_application.c.auto,
            payment_application.c.obligation_id.is_not(None),
        )
    ).all()
    for row in rows:
        record_event(
            uow,
            EVENT_MODELS,
            PAYMENT_DETECTED,
            refs.format_ref(refs.FINANCE_SYSTEM, PAYMENT_TYPE, row.payment_id),
            row.paid_on,
            PaymentDetected(
                payee_ref=refs.format_ref(
                    refs.FINANCE_SYSTEM, PAYEE_TYPE, row.payee_id
                ),
                obligation_ref=refs.format_ref(
                    refs.FINANCE_SYSTEM, OBLIGATION_TYPE, row.obligation_id
                ),
            ),
            key=(row.obligation_id,),
        )
    return len(rows)
