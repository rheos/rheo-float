"""Derivation stage 3, part two: receipts and invoices (FR-8, FR-16; SPEC "Derivation"
stage 3, "Receipts and invoices"; Assumptions, matching windows).

A ``receipt`` or ``invoice`` line of a parsed document has no account: it is evidence
of a payment, not a payment. Once part one (``stage3``) has written every txn of the
statements, exports and manual cash lines, each such line is matched ONE to one
against those txns, the payment lines:

- **What matches.** The same currency and signed amount (both are the owner's
  outflow, negative), a posting or transaction date of the txn within the window of
  the line's date (``finance.receipts.match_days`` for a receipt,
  ``finance.invoices.match_days`` for an invoice, either side of it), and the payee:
  the line's ``counterparty``, normalized as descriptions are, appears in the txn's
  normalized description as whole words. Prompt 48 replaces the payee rule with the
  account ``payee_patterns`` and ``payees.csv`` patterns.
- **Which one.** Lines are taken oldest first; a line takes a txn on its exact date
  before one inside the window, then the nearest date, then the earliest txn. Within
  one kind a txn takes at most one line, so two receipts of one amount never share a
  payment. A receipt and an invoice of the same payment both attach to it: matching
  is one to one within each kind, so a paid invoice never pushes its receipt out to
  "Paid outside recorded accounts" and counts it twice.
- **A match** adds a ``txn_source`` row with role ``evidence`` on the payment txn and
  adds nothing to spending.
- **An unmatched receipt** becomes its own txn on the internal account "Paid outside
  recorded accounts" at its date (a receipt proves payment): origin
  ``document_expense``, basis ``unverified_export`` (a receipt states no balance),
  the receipt line its primary source.
- **An unmatched invoice** becomes no txn, however old. It is left for stage 17's
  open items and the ``invoice_paid`` and ``invoice_unpaid`` decisions.

**Keys.** A receipt or invoice line is keyed by (kind, payee, date, signed amount,
currency, normalized description, occurrence among identical lines of its document),
so its key depends on no other document and never meets a fiat or manual key. A
second copy of a document (a re-saved file, which stage 1 does not supersede because
these documents state no period) gives the same keys: lines of one key are one
receipt or invoice, matched once. The copies are ``evidence`` sources of the txn the
first one reached, so nothing counts twice. Every keyed line stores its key and
normalized description on ``document_line``, as part one does.
"""

from __future__ import annotations

import datetime as dt
import re
import uuid
from collections import Counter, defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Final, Literal

from sqlalchemy import bindparam, insert, select, update

from rheo_finance.context import UnitOfWork
from rheo_finance.manual.accounts import INTERNAL_ACCOUNT
from rheo_finance.manual.stage2 import account_id
from rheo_finance.storage.ids import derived_id
from rheo_finance.storage.schema import document, document_line, txn, txn_source
from rheo_finance.text import normalize_description

Kind = Literal["receipt", "invoice"]
KINDS: Final[tuple[Kind, ...]] = ("receipt", "invoice")
RECEIPT_ORIGIN: Final = "document_expense"
RECEIPT_BASIS: Final = "unverified_export"


@dataclass(frozen=True)
class Evidence:
    """What part two wrote, as counts."""

    # ``evidence`` sources: lines that matched a payment, and copies of a receipt.
    links: int = 0
    # Unmatched receipts, each a txn on "Paid outside recorded accounts".
    receipt_txns: int = 0
    # Invoices no payment line matched: no txn, left for the open items.
    unmatched_invoices: int = 0
    # Receipt or invoice lines without a date, an amount or a currency, left out.
    incomplete_lines: int = 0


@dataclass(frozen=True)
class Line:
    """One receipt or invoice line with its key."""

    id: uuid.UUID
    kind: Kind
    posting_date: dt.date
    amount: Decimal
    currency: str
    description_norm: str
    payee: str
    key: uuid.UUID


@dataclass(frozen=True)
class Payment:
    """A txn part one wrote, as a match candidate."""

    id: uuid.UUID
    dates: tuple[dt.date, ...]
    posting_date: dt.date
    amount: Decimal
    currency: str
    description_norm: str


def evidence_key(
    kind: str,
    payee: str,
    posting_date: dt.date,
    amount: Decimal,
    currency: str,
    description_norm: str,
    occurrence: int,
) -> uuid.UUID:
    """The id of a receipt or invoice line (also the id of an unmatched receipt's
    txn)."""
    return derived_id(
        posting_date,
        (kind, payee, posting_date, amount, currency, description_norm, occurrence),
    )


def names_payee(payee: str, description_norm: str) -> bool:
    """Whether the normalized ``payee`` appears as whole words in a normalized
    description. An empty payee names nothing."""
    norm = normalize_description(payee)
    if not norm:
        return False
    return re.search(rf"(?<!\w){re.escape(norm)}(?!\w)", description_norm) is not None


def _gap(day: dt.date, payment: Payment) -> int:
    return min(abs((day - each).days) for each in payment.dates)


def match(
    groups: Sequence[Line], payments: Sequence[Payment], days: int
) -> dict[uuid.UUID, uuid.UUID]:
    """Match one kind's lines (one per key) one to one against ``payments``: a line's
    key to the payment txn it takes. Pure."""
    by_amount: dict[tuple[str, Decimal], list[Payment]] = defaultdict(list)
    for payment in payments:
        by_amount[payment.currency, payment.amount].append(payment)
    ordered = sorted(groups, key=lambda line: line.posting_date)
    taken: set[uuid.UUID] = set()
    found: dict[uuid.UUID, uuid.UUID] = {}
    for limit in (0, days):
        for line in ordered:
            if line.key in found:
                continue
            candidates = [
                (_gap(line.posting_date, payment), payment.posting_date, payment.id)
                for payment in by_amount.get((line.currency, line.amount), ())
                if payment.id not in taken
                and names_payee(line.payee, payment.description_norm)
            ]
            fitting = [each for each in candidates if each[0] <= limit]
            if fitting:
                best = min(fitting)[2]
                taken.add(best)
                found[line.key] = best
    return found


def _lines(uow: UnitOfWork) -> tuple[dict[uuid.UUID, list[Line]], int]:
    """Every receipt and invoice line of a parsed document, grouped by key; within a
    group, the larger document sha256 first, then line order."""
    rows = uow.execute(
        select(
            document_line.c.id,
            document_line.c.document_id,
            document_line.c.line_kind,
            document_line.c.posting_date,
            document_line.c.amount,
            document_line.c.currency,
            document_line.c.description_raw,
            document_line.c.counterparty,
        )
        .join(document, document.c.id == document_line.c.document_id)
        .where(
            document.c.status == "parsed",
            document_line.c.line_kind.in_(KINDS),
        )
        .order_by(document.c.sha256.desc(), document_line.c.line_no)
    ).all()
    occurrences: dict[uuid.UUID, Counter[tuple[object, ...]]] = defaultdict(Counter)
    groups: dict[uuid.UUID, list[Line]] = {}
    incomplete = 0
    for row in rows:
        if row.posting_date is None or row.amount is None or row.currency is None:
            incomplete += 1
            continue
        norm = normalize_description(row.description_raw or "")
        payee = normalize_description(row.counterparty or "")
        natural = (
            row.line_kind, payee, row.posting_date, row.amount, row.currency, norm,
        )  # fmt: skip
        seen = occurrences[row.document_id]
        seen[natural] += 1
        key = evidence_key(*natural, seen[natural])
        line = Line(
            id=row.id,
            kind=row.line_kind,
            posting_date=row.posting_date,
            amount=row.amount,
            currency=row.currency,
            description_norm=norm,
            payee=payee,
            key=key,
        )
        groups.setdefault(key, []).append(line)
    return groups, incomplete


def _payments(uow: UnitOfWork) -> list[Payment]:
    rows = uow.execute(
        select(
            txn.c.id,
            txn.c.posting_date,
            txn.c.txn_date,
            txn.c.amount,
            txn.c.currency,
            txn.c.description_norm,
        )
    )
    return [
        Payment(
            id=row.id,
            dates=(row.posting_date,)
            if row.txn_date is None
            else (row.posting_date, row.txn_date),
            posting_date=row.posting_date,
            amount=row.amount,
            currency=row.currency,
            description_norm=row.description_norm or "",
        )
        for row in rows
    ]


def _source(txn_id: uuid.UUID, line: Line, role: str) -> dict[str, object]:
    return {
        "txn_id": txn_id,
        "document_line_id": line.id,
        "manual_ref": None,
        "line_key_id": line.key,
        "role": role,
    }


def link(uow: UnitOfWork, *, receipt_days: int, invoice_days: int) -> Evidence:
    """Match every receipt and invoice line against the txns already written, and
    write the evidence sources and the unmatched receipts' txns. ``receipt_days`` is
    ``finance.receipts.match_days`` and ``invoice_days`` is
    ``finance.invoices.match_days``."""
    groups, incomplete = _lines(uow)
    payments = _payments(uow)
    windows: dict[Kind, int] = {"receipt": receipt_days, "invoice": invoice_days}
    found: dict[uuid.UUID, uuid.UUID] = {}
    for kind in KINDS:
        firsts = [lines[0] for lines in groups.values() if lines[0].kind == kind]
        found.update(match(firsts, payments, windows[kind]))

    paid_outside = account_id(INTERNAL_ACCOUNT.slug)
    txns: list[dict[str, object]] = []
    sources: list[dict[str, object]] = []
    unmatched_invoices = 0
    for key, lines in groups.items():
        first, copies = lines[0], lines[1:]
        if key in found:
            sources.extend(_source(found[key], each, "evidence") for each in lines)
        elif first.kind == "receipt":
            txns.append(
                {
                    "id": key,
                    "account_id": paid_outside,
                    "posting_date": first.posting_date,
                    "amount": first.amount,
                    "currency": first.currency,
                    "description_norm": first.description_norm,
                    "counterparty": first.payee or None,
                    "origin": RECEIPT_ORIGIN,
                    "basis": RECEIPT_BASIS,
                    "movement_kind": "fiat",
                }
            )
            sources.append(_source(key, first, "primary"))
            sources.extend(_source(key, each, "evidence") for each in copies)
        else:
            unmatched_invoices += 1
    if txns:
        uow.execute(insert(txn), txns)
    if sources:
        uow.execute(insert(txn_source), sources)
    keyed = [line for lines in groups.values() for line in lines]
    if keyed:
        uow.execute(
            update(document_line)
            .where(document_line.c.id == bindparam("b_id"))
            .values(
                description_norm=bindparam("b_norm"), line_key_id=bindparam("b_key")
            ),
            [
                {"b_id": line.id, "b_norm": line.description_norm, "b_key": line.key}
                for line in keyed
            ],
        )
    return Evidence(
        links=sum(1 for each in sources if each["role"] == "evidence"),
        receipt_txns=len(txns),
        unmatched_invoices=unmatched_invoices,
        incomplete_lines=incomplete,
    )
