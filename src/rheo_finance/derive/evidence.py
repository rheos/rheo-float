"""Derivation stage 3, part two: receipts and invoices (FR-8, FR-16; SPEC "Derivation"
stage 3, "Receipts and invoices"; Assumptions, matching windows).

A ``receipt`` or ``invoice`` line of a parsed document has no account: it is evidence
of a payment, not a payment. Once part one (``stage3``) has written every txn of the
statements, exports and manual cash lines, each such line is matched ONE to one
against payment lines:

- **What can match.** The same currency and signed amount (both are the owner's
  outflow, negative), a posting or transaction date of the txn within the window of
  the line's date (``finance.receipts.match_days`` for a receipt,
  ``finance.invoices.match_days`` for an invoice, either side of it), and the payee:
  the line's ``counterparty``, normalized as descriptions are, appears as whole words
  in the txn's normalized description or its counterparty. Prompt 48 replaces the
  payee rule with the account ``payee_patterns`` and ``payees.csv`` patterns.
- **Which pairs.** Within each kind the matching is maximal: as many lines as can be
  matched one to one are, so a greedy choice never strands a receipt that had a
  payment of its own (that would count it twice). Among the maximal matchings the one
  with the least total date distance wins; the search runs over lines and payments in
  a fixed order (date, then id), so the same input always gives the same pairs.
  Matching is one to one within each kind and not across kinds: a receipt and an
  invoice of the same payment both attach to it, so a paid invoice never pushes its
  receipt out to "Paid outside recorded accounts" and counts it twice.
- **A match** adds a ``txn_source`` row with role ``evidence`` on the payment txn and
  adds nothing to spending.
- **An unmatched receipt** becomes its own txn on the internal account "Paid outside
  recorded accounts" at its date (a receipt proves payment): origin
  ``document_expense``, basis ``unverified_export`` (a receipt states no balance),
  the receipt line its primary source. The internal account takes CAD only; an
  unmatched receipt in another currency is refused and counted, never booked there
  (as a manual cash line in a currency its account does not take is).
- **Invoices match after the receipts are booked**, so an invoice may attach to the
  txn of an unmatched receipt (an invoice paid in cash with a receipt for it): the
  invoice is then evidence of that txn and stays open nowhere.
- **An unmatched invoice** becomes no txn, however old. It is left for stage 17's
  open items and the ``invoice_paid`` and ``invoice_unpaid`` decisions.

**Keys.** A receipt or invoice line is keyed by (kind, payee, date, signed amount,
currency, normalized description, the payment's reference digest, occurrence among
identical lines of its document), so its key depends on no other document and never
meets a fiat or manual key. The reference digest (``payment_ref_digest``, which the
receipt parser takes from the number a receipt prints for each payment) keeps two
genuine receipts that print the same date, amount and type apart. A second copy of
one document (a re-saved file, which stage 1 does not supersede because these
documents state no period) gives the same keys: lines of one key are one receipt or
invoice, matched once. The line of the document with the larger sha256 is the one
that counts (whatever order the copies were filed in); the copies are ``evidence``
sources of the txn it reached, so nothing counts twice. Every keyed line stores its
key and normalized description on ``document_line``, as part one does.
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
    # Unmatched receipts in a currency the internal account does not take: no txn.
    receipts_currency_refused: int = 0
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
    """A txn as a match candidate."""

    id: uuid.UUID
    dates: tuple[dt.date, ...]
    posting_date: dt.date
    amount: Decimal
    currency: str
    # The normalized texts the payee is looked for in: description, counterparty.
    texts: tuple[str, ...]


def evidence_key(
    kind: str,
    payee: str,
    posting_date: dt.date,
    amount: Decimal,
    currency: str,
    description_norm: str,
    ref_digest: str,
    occurrence: int,
) -> uuid.UUID:
    """The id of a receipt or invoice line (also the id of an unmatched receipt's
    txn)."""
    return derived_id(
        posting_date,
        (
            kind, payee, posting_date, amount, currency, description_norm,
            ref_digest, occurrence,
        ),
    )  # fmt: skip


def names_payee(payee: str, texts: Sequence[str]) -> bool:
    """Whether the normalized ``payee`` appears as whole words in one of ``texts``
    (normalized). An empty payee names nothing."""
    norm = normalize_description(payee)
    if not norm:
        return False
    pattern = re.compile(rf"(?<!\w){re.escape(norm)}(?!\w)")
    return any(pattern.search(normalize_description(text)) for text in texts)


def _gap(day: dt.date, payment: Payment) -> int:
    return min(abs((day - each).days) for each in payment.dates)


def match(
    lines: Sequence[Line], payments: Sequence[Payment], days: int
) -> dict[uuid.UUID, uuid.UUID]:
    """Match one kind's lines (one per key) one to one against ``payments``: a line's
    key to the payment txn it takes. Pure.

    A maximum matching of least total date distance, by successive shortest
    augmenting paths: each round adds one pair along the cheapest path from an
    unmatched line to an unmatched payment, which may move lines already matched to
    other payments. Each round's matching is the cheapest of its size, and the rounds
    stop when no path is left, so the last is maximal. Lines and payments are taken
    in (date, id) order and only a strictly shorter path replaces a found one, so ties
    resolve the same way every run."""
    ordered = sorted(lines, key=lambda line: (line.posting_date, line.key))
    candidates = sorted(payments, key=lambda each: (each.posting_date, each.id))
    by_amount: dict[tuple[str, Decimal], list[int]] = defaultdict(list)
    for index, payment in enumerate(candidates):
        by_amount[payment.currency, payment.amount].append(index)
    # edges[line] = [(payment, date distance)], in payment order.
    edges: list[list[tuple[int, int]]] = []
    for item in ordered:
        found: list[tuple[int, int]] = []
        for index in by_amount.get((item.currency, item.amount), ()):
            payment = candidates[index]
            gap = _gap(item.posting_date, payment)
            if gap <= days and names_payee(item.payee, payment.texts):
                found.append((index, gap))
        edges.append(found)
    cost = [dict(each) for each in edges]

    line_of: dict[int, int] = {}  # payment -> line
    payment_of: dict[int, int] = {}  # line -> payment
    while True:
        # Bellman-Ford from every unmatched line over the residual graph: a line
        # reaches a matched payment's line by taking that payment from it.
        dist: dict[int, int] = {
            line: 0 for line in range(len(ordered)) if line not in payment_of
        }
        via: dict[int, tuple[int, int]] = {}
        for _ in range(len(ordered) + 1):
            changed = False
            for line in range(len(ordered)):
                if line not in dist:
                    continue
                for index, gap in edges[line]:
                    holder = line_of.get(index)
                    if holder is None or holder == line:
                        continue
                    reach = dist[line] + gap - cost[holder][index]
                    if holder not in dist or reach < dist[holder]:
                        dist[holder] = reach
                        via[holder] = (line, index)
                        changed = True
            if not changed:
                break
        best: tuple[int, int, int] | None = None  # (total, line, payment)
        for line in range(len(ordered)):
            if line not in dist:
                continue
            for index, gap in edges[line]:
                if index in line_of:
                    continue
                total = dist[line] + gap
                if best is None or total < best[0]:
                    best = (total, line, index)
        if best is None:
            break
        _, line, index = best
        while True:
            payment_of[line] = index
            line_of[index] = line
            if line not in via:
                break
            line, index = via[line]
    return {
        ordered[line].key: candidates[index].id for line, index in payment_of.items()
    }


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
            document_line.c.payment_ref_digest,
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
            row.payment_ref_digest or "",
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
            txn.c.counterparty,
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
            texts=(row.description_norm or "", row.counterparty or ""),
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


def _evidence(
    found: dict[uuid.UUID, uuid.UUID], groups: dict[uuid.UUID, list[Line]]
) -> list[dict[str, object]]:
    return [
        _source(found[key], each, "evidence")
        for key, lines in groups.items()
        if key in found
        for each in lines
    ]


def link(uow: UnitOfWork, *, receipt_days: int, invoice_days: int) -> Evidence:
    """Match every receipt and invoice line against the txns already written, and
    write the evidence sources and the unmatched receipts' txns. ``receipt_days`` is
    ``finance.receipts.match_days`` and ``invoice_days`` is
    ``finance.invoices.match_days``."""
    groups, incomplete = _lines(uow)
    by_kind: dict[Kind, dict[uuid.UUID, list[Line]]] = {kind: {} for kind in KINDS}
    for key, lines in groups.items():
        by_kind[lines[0].kind][key] = lines
    payments = _payments(uow)

    receipts = by_kind["receipt"]
    found = match([lines[0] for lines in receipts.values()], payments, receipt_days)
    sources = _evidence(found, receipts)
    paid_outside = account_id(INTERNAL_ACCOUNT.slug)
    txns: list[dict[str, object]] = []
    refused = 0
    for key, lines in receipts.items():
        if key in found:
            continue
        first, copies = lines[0], lines[1:]
        if first.currency not in INTERNAL_ACCOUNT.currencies:
            refused += 1
            continue
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
        payments.append(
            Payment(
                id=key,
                dates=(first.posting_date,),
                posting_date=first.posting_date,
                amount=first.amount,
                currency=first.currency,
                texts=(first.description_norm, first.payee),
            )
        )

    invoices = by_kind["invoice"]
    paid = match([lines[0] for lines in invoices.values()], payments, invoice_days)
    sources.extend(_evidence(paid, invoices))

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
        receipts_currency_refused=refused,
        unmatched_invoices=len(invoices) - len(paid),
        incomplete_lines=incomplete,
    )
