"""Derivation stage 14: the obligations (FR-19, FR-21; Key decision 20; SPEC Data Models
``obligation``). Nothing outside this stage creates one, and a card charge from a
subscription vendor is a spending line only.

Two of the five paths of FR-19 are built:

- (1) A card statement: a parsed statement of a ``credit_card`` account gives
  ``card_statement``, period ``card-YYYY-MM-DD`` of its closing date, issued on it.
  ``threshold_due`` is the printed minimum, else the full balance.
- (2) A parsed bill: a parsed document of kind ``bill`` gives ``bill``, period
  ``bill-YYYY-MM`` of its due month, issued on its printed bill date. A later bill of
  one payee due in one month takes ``-2``, ``-3`` in (bill date, due date, path) order,
  which ``report_status`` counts for status line 9. A parsed bill that prints no total
  due has no amount to owe and makes no obligation.

A document ``precedence.supersede`` marked as a copy is not parsed, so it makes nothing.
Two different parsed statements of one card with one closing date have one natural key,
so they are one obligation: the last in path order.

**The payee.** The payee standing for the account (``payee.account_id``), else the
payee named by the account's slug for a card or the issuer's slug for a bill. Stage 2
makes both of those exist (``manual.payees``), so a parse with no payee fails the derive
rather than dropping what is owed.

**Supersession (FR-21).** Per (payee, kind), in the order above, each newer row names
the one before it in ``supersedes_id`` and the one before it is ``superseded``. A
statement's balance and minimum already hold what the previous one carried, and a bill's
total holds the unpaid balance before it, so only the newest is owed.

No payment is applied yet: ``paid`` is zero, and a row with nothing remaining (a card
at zero or in credit) is ``paid`` rather than open.

The id comes from the natural key alone, on the fixed registry date, so a figure or a
date that changes keeps the id a decision names.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from decimal import Decimal
from itertools import pairwise
from typing import Final, Literal

from sqlalchemy import Row, insert, select

from rheo_finance.context import UnitOfWork
from rheo_finance.derive.stage4 import cents
from rheo_finance.manual.accounts import CREDIT_CARD
from rheo_finance.manual.obligations import bill_period
from rheo_finance.manual.stage2 import REGISTRY_DATE
from rheo_finance.storage.ids import derived_id
from rheo_finance.storage.schema import (
    account,
    document,
    obligation,
    payee,
    statement_period,
)

ObligationKind = Literal[
    "card_statement", "bill", "arrangement_instalment", "arrears", "manual_debt"
]

CARD_STATEMENT: Final[ObligationKind] = "card_statement"
BILL: Final[ObligationKind] = "bill"
CURRENCY: Final = "CAD"
ZERO: Final = Decimal("0.00")

_PayeeOf = Callable[[Row[tuple[object, ...]], str], uuid.UUID]


def obligation_id(
    payee_id: uuid.UUID, kind: ObligationKind, period_label: str
) -> uuid.UUID:
    """The id of the obligation with this natural key."""
    return derived_id(REGISTRY_DATE, ("obligation", payee_id, kind, period_label))


@dataclass(frozen=True)
class _Draft:
    """One obligation before its place in its payee's chain is known."""

    payee_id: uuid.UUID
    kind: ObligationKind
    period_label: str
    statement_date: dt.date | None
    issued_on: dt.date
    amount_due: Decimal
    minimum_due: Decimal | None
    due_date: dt.date | None
    source: str

    @property
    def id(self) -> uuid.UUID:
        return obligation_id(self.payee_id, self.kind, self.period_label)


def _periods(uow: UnitOfWork) -> list[Row[tuple[object, ...]]]:
    """The parsed periods that state an amount owed, with their document and account.
    Only ``CURRENCY``: every card and bill parser prints it, and ``amount_due`` is
    CAD."""
    sp = statement_period.c
    return list(
        uow.execute(
            select(
                sp.account_id,
                sp.period_end,
                sp.balance_due,
                sp.minimum_due,
                sp.due_date,
                sp.bill_date,
                document.c.path,
                document.c.sha256,
                document.c.kind,
                document.c.issuer,
                account.c.slug.label("account_slug"),
                account.c.kind.label("account_kind"),
            )
            .select_from(
                statement_period.join(document, sp.document_id == document.c.id).join(
                    account, sp.account_id == account.c.id
                )
            )
            .where(
                document.c.status == "parsed",
                sp.currency == CURRENCY,
                sp.balance_due.is_not(None),
            )
            .order_by(sp.period_end, document.c.path, document.c.sha256)
        ).all()
    )


def _payees(
    uow: UnitOfWork,
) -> tuple[Mapping[uuid.UUID, uuid.UUID], Mapping[str, uuid.UUID]]:
    """Payee ids by the account each stands for, and by slug."""
    rows = uow.execute(select(payee.c.id, payee.c.slug, payee.c.account_id)).all()
    return (
        {row.account_id: row.id for row in rows if row.account_id is not None},
        {row.slug: row.id for row in rows},
    )


def _card_drafts(
    rows: Iterable[Row[tuple[object, ...]]], payee_of: _PayeeOf
) -> list[_Draft]:
    """One draft per natural key, in ``rows`` order; the last row of a key holds it."""
    drafts: dict[tuple[uuid.UUID, str], _Draft] = {}
    for row in rows:
        payee_id = payee_of(row, row.account_slug)
        label = f"card-{row.period_end:%Y-%m-%d}"
        drafts[(payee_id, label)] = _Draft(
            payee_id=payee_id,
            kind=CARD_STATEMENT,
            period_label=label,
            statement_date=row.period_end,
            issued_on=row.period_end,
            amount_due=cents(row.balance_due),
            minimum_due=None if row.minimum_due is None else cents(row.minimum_due),
            due_date=row.due_date,
            source=row.path,
        )
    return list(drafts.values())


def _bill_drafts(
    rows: Iterable[Row[tuple[object, ...]]], payee_of: _PayeeOf
) -> list[_Draft]:
    """A draft per bill, in the order that numbers a due month's second bill."""
    ordered = sorted(
        rows, key=lambda row: (row.bill_date, row.due_date, row.path, row.sha256)
    )
    seen: Counter[tuple[uuid.UUID, str]] = Counter()
    drafts = []
    for row in ordered:
        payee_id = payee_of(row, row.issuer)
        month = bill_period(row.due_date)
        seen[(payee_id, month)] += 1
        place = seen[(payee_id, month)]
        drafts.append(
            _Draft(
                payee_id=payee_id,
                kind=BILL,
                period_label=month if place == 1 else f"{month}-{place}",
                statement_date=None,
                issued_on=row.bill_date,
                amount_due=cents(row.balance_due),
                minimum_due=None,
                due_date=row.due_date,
                source=row.path,
            )
        )
    return drafts


def _values(
    draft: _Draft, *, supersedes: _Draft | None, superseded: bool
) -> dict[str, object]:
    threshold = draft.amount_due if draft.minimum_due is None else draft.minimum_due
    remaining = max(draft.amount_due, ZERO)
    status = "superseded" if superseded else "open" if remaining else "paid"
    return {
        "id": draft.id,
        "payee_id": draft.payee_id,
        "kind": draft.kind,
        "period_label": draft.period_label,
        "statement_date": draft.statement_date,
        "issued_on": draft.issued_on,
        "amount_due": draft.amount_due,
        "minimum_due": draft.minimum_due,
        "threshold_due": threshold,
        "paid": ZERO,
        "remaining": remaining,
        "threshold_remaining": max(threshold, ZERO),
        "due_date": draft.due_date,
        "status": status,
        "primary_source": draft.source,
        "manual_mismatch": None,
        "supersedes_id": None if supersedes is None else supersedes.id,
        "arrangement_ref": None,
        "invoice_ref": None,
        "payments_mismatch": None,
    }


def _chained(drafts: Iterable[_Draft]) -> list[dict[str, object]]:
    """Every draft's row: oldest to newest per (payee, kind), each newer one superseding
    the one before it. ``drafts`` comes in that order."""
    chains: dict[tuple[uuid.UUID, ObligationKind], list[_Draft]] = defaultdict(list)
    for draft in drafts:
        chains[(draft.payee_id, draft.kind)].append(draft)
    rows: list[dict[str, object]] = []
    for chain in chains.values():
        before: dict[uuid.UUID, _Draft] = {
            newer.id: older for older, newer in pairwise(chain)
        }
        newest = chain[-1].id
        rows.extend(
            _values(each, supersedes=before.get(each.id), superseded=each.id != newest)
            for each in chain
        )
    return rows


def create_obligations(uow: UnitOfWork) -> int:
    """Rebuild ``obligation`` from the parsed statements and bills; how many it made.
    The caller cleared the table."""
    by_account, by_slug = _payees(uow)

    def payee_of(row: Row[tuple[object, ...]], slug: str) -> uuid.UUID:
        return by_account.get(row.account_id) or by_slug[slug]

    periods = _periods(uow)
    cards = [
        row
        for row in periods
        if row.account_kind == CREDIT_CARD and row.kind == "statement"
    ]
    bills = [row for row in periods if row.kind == "bill"]
    rows = _chained([*_card_drafts(cards, payee_of), *_bill_drafts(bills, payee_of)])
    if rows:
        uow.execute(insert(obligation), rows)
    return len(rows)
