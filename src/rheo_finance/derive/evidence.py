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
  one of the line's payee patterns is a whole-token run of the txn's description, both
  folded (``manual.payees.compared``: case, punctuation and whitespace). A line's payee
  patterns are its printed payee (its ``counterparty``) and the patterns of every payee
  one of whose patterns names that printed payee (``manual.payees``: the ``payees.csv``
  rows and the account-backed payees of ``accounts.csv`` ``payee_patterns``), so a
  receipt whose payee prints one name and whose card line prints another matches once
  the owner registers both as one payee. Only the txn of an unmatched receipt is also
  searched by its counterparty (the receipt's payee), so an invoice can attach to a
  receipt paid in cash; a statement or export line's counterparty is never read.
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
- **The owner's decision** on an invoice (``decision.effective``, the newest unreversed
  of the two kinds, on ``finance.invoice:<line key>``) applies only while no payment
  matches it. ``invoice_paid`` books it like an unmatched receipt: a txn on the internal
  account, at the decided date, origin ``document_expense``, the invoice line its
  primary source; the paying account stays on the decision. ``invoice_unpaid`` writes
  nothing here: the invoice is handed to stage 14 (``UnpaidInvoice``), which makes the
  debt, for the printed amount due, else the total. Either way the invoice is no longer
  open. Reversing the decision returns it to the open state on the next derive. An
  invoice in a currency the internal account does not take is refused and stays open,
  because neither a txn there nor an obligation can hold it: the line names the decision
  in ``refused_decision_id``, which status line 9 counts.
- **A match beats a decision**, so an invoice never counts twice: the payment takes the
  invoice as evidence, the decision adds nothing, and the evidence source names it in
  ``overrides_decision_id``, which status line 9 counts. A receipt's txn is a payment
  here, so an invoice paid in cash with a receipt stays on that one txn. An owner who
  decides an invoice was paid months after its date can have the payment line arrive
  later, outside the window of the invoice's date, so an ``invoice_paid`` invoice left
  unmatched is matched once more, by the decided date, against the payments on the
  decided account that no invoice took. That round runs after the first, so a decision
  moves no other invoice's match.

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
import uuid
from collections import Counter, defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace
from decimal import Decimal
from typing import Final, Literal

from sqlalchemy import bindparam, insert, select, update

from rheo_finance.context import UnitOfWork
from rheo_finance.decision import (
    INVOICE_DECISIONS,
    Decision,
    DecisionKind,
    effective,
    invoice_ref,
)
from rheo_finance.manual.accounts import INTERNAL_ACCOUNT
from rheo_finance.manual.payees import Payees, names_any
from rheo_finance.manual.stage2 import account_id
from rheo_finance.parsers.base import FIAT
from rheo_finance.storage import refs
from rheo_finance.storage.ids import derived_id
from rheo_finance.storage.schema import document, document_line, txn, txn_source
from rheo_finance.text import normalize_description

Kind = Literal["receipt", "invoice"]
KINDS: Final[tuple[Kind, ...]] = ("receipt", "invoice")
DOCUMENT_ORIGIN: Final = "document_expense"
DOCUMENT_BASIS: Final = "unverified_export"


@dataclass(frozen=True)
class UnpaidInvoice:
    """An invoice no payment matched whose decision in force is ``invoice_unpaid``, the
    copy that counts, for stage 14."""

    line_key: uuid.UUID
    invoice_date: dt.date
    # The printed amount due, else the total; positive.
    owed: Decimal
    payee: str
    document_id: uuid.UUID
    path: str
    issuer: str
    due_date: dt.date | None


@dataclass(frozen=True)
class Evidence:
    """What part two wrote, as counts."""

    # ``evidence`` sources: lines that matched a payment, and copies of a receipt.
    links: int = 0
    # Unmatched receipts, each a txn on "Paid outside recorded accounts".
    receipt_txns: int = 0
    # Unmatched receipts in a currency the internal account does not take: no txn.
    receipts_currency_refused: int = 0
    # Invoices no payment line matched and no decision settled: no txn, left for the
    # open items.
    unmatched_invoices: int = 0
    # Invoices an ``invoice_paid`` decision booked, each a txn on the internal account.
    invoice_txns: int = 0
    # Decided invoices in a currency the internal account does not take: still open.
    invoices_currency_refused: int = 0
    # Invoice decisions a matching payment took over from: they add nothing.
    decisions_superseded: int = 0
    # The invoices stage 14 turns into debts.
    unpaid_invoices: tuple[UnpaidInvoice, ...] = ()
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
    # The normalized names the payment's text must hold one of: the printed payee
    # first, then the patterns of the registered payees that name it.
    patterns: tuple[str, ...]
    document_id: uuid.UUID
    path: str
    issuer: str
    # What an invoice prints as still due, when it does.
    amount_due: Decimal | None


@dataclass(frozen=True)
class Payment:
    """A txn as a match candidate."""

    id: uuid.UUID
    account_id: uuid.UUID
    dates: tuple[dt.date, ...]
    posting_date: dt.date
    amount: Decimal
    currency: str
    # The normalized texts the payee is looked for in: the description, and for an
    # unmatched receipt's txn its payee too.
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


def names_payee(patterns: Sequence[str], texts: Sequence[str]) -> bool:
    """Whether one of ``patterns`` is a whole-token run of one of ``texts``, both
    folded. No pattern, or only empty ones, names nothing."""
    return any(names_any(patterns, text) for text in texts)


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
            if gap <= days and names_payee(item.patterns, payment.texts):
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


def _lines(uow: UnitOfWork, payees: Payees) -> tuple[dict[uuid.UUID, list[Line]], int]:
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
            document_line.c.amount_due,
            document.c.path,
            document.c.issuer,
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
            patterns=_patterns(payee, payees),
            document_id=row.document_id,
            path=row.path,
            issuer=row.issuer,
            amount_due=row.amount_due,
        )
        groups.setdefault(key, []).append(line)
    return groups, incomplete


def _patterns(payee: str, payees: Payees) -> tuple[str, ...]:
    """The printed ``payee`` (normalized), then the patterns of the registered payees
    that name it, without repeats or empties."""
    found = [payee] if payee else []
    found.extend(p for p in payees.patterns_naming(payee) if p not in found)
    return tuple(found)


def _payments(uow: UnitOfWork) -> list[Payment]:
    rows = uow.execute(
        select(
            txn.c.id,
            txn.c.account_id,
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
            account_id=row.account_id,
            dates=(row.posting_date,)
            if row.txn_date is None
            else (row.posting_date, row.txn_date),
            posting_date=row.posting_date,
            amount=row.amount,
            currency=row.currency,
            texts=(row.description_norm or "",),
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
        "overrides_decision_id": None,
    }


def _paid_outside(line: Line, on: dt.date) -> dict[str, object]:
    """The internal account's txn of an unmatched receipt or a decided invoice."""
    return {
        "id": line.key,
        "account_id": account_id(INTERNAL_ACCOUNT.slug),
        "posting_date": on,
        "amount": line.amount,
        "currency": line.currency,
        "description_norm": line.description_norm,
        "counterparty": line.payee or None,
        "origin": DOCUMENT_ORIGIN,
        "basis": DOCUMENT_BASIS,
        "movement_kind": FIAT,
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


def _invoice_decisions(
    uow: UnitOfWork, invoices: Iterable[uuid.UUID]
) -> dict[uuid.UUID, Decision]:
    """The decision in force on each invoice that has one, by line key."""
    found = {
        key: effective(uow, INVOICE_DECISIONS, invoice_ref(key)) for key in invoices
    }
    return {key: each for key, each in found.items() if each is not None}


def _decided_date(decided: Decision) -> dt.date:
    assert decided.effective_on is not None  # invoice_paid requires it
    return decided.effective_on


def _decided_account(decided: Decision) -> uuid.UUID:
    assert decided.account_ref is not None  # invoice_paid requires it
    return uuid.UUID(refs.parse(decided.account_ref).id)


def _matched_on_decided_date(
    invoices: dict[uuid.UUID, list[Line]],
    decided: dict[uuid.UUID, Decision],
    paid: dict[uuid.UUID, uuid.UUID],
    payments: Sequence[Payment],
    days: int,
) -> dict[uuid.UUID, uuid.UUID]:
    """The ``invoice_paid`` invoices no payment matched by their own date, matched by
    the decided date against the payments on the decided account that no invoice took.
    One account's payments are not another's, so each account is its own matching."""
    taken = set(paid.values())
    by_account: dict[uuid.UUID, list[Line]] = defaultdict(list)
    for key, each in decided.items():
        if key not in paid and each.kind is DecisionKind.INVOICE_PAID:
            by_account[_decided_account(each)].append(
                replace(invoices[key][0], posting_date=_decided_date(each))
            )
    found: dict[uuid.UUID, uuid.UUID] = {}
    for paying, lines in sorted(by_account.items()):
        found |= match(
            lines,
            [
                each
                for each in payments
                if each.account_id == paying and each.id not in taken
            ],
            days,
        )
    return found


def _unpaid(line: Line, decided: Decision) -> UnpaidInvoice:
    return UnpaidInvoice(
        line_key=line.key,
        invoice_date=line.posting_date,
        owed=-line.amount if line.amount_due is None else line.amount_due,
        payee=line.payee,
        document_id=line.document_id,
        path=line.path,
        issuer=line.issuer,
        due_date=decided.effective_on,
    )


def link(
    uow: UnitOfWork,
    *,
    receipt_days: int,
    invoice_days: int,
    payees: Payees,
) -> Evidence:
    """Match every receipt and invoice line against the txns already written, and
    write the evidence sources and the unmatched receipts' txns. ``receipt_days`` is
    ``finance.receipts.match_days``, ``invoice_days`` is
    ``finance.invoices.match_days``, and ``payees`` the payees stage 2 loaded."""
    groups, incomplete = _lines(uow, payees)
    by_kind: dict[Kind, dict[uuid.UUID, list[Line]]] = {kind: {} for kind in KINDS}
    for key, lines in groups.items():
        by_kind[lines[0].kind][key] = lines
    payments = _payments(uow)

    receipts = by_kind["receipt"]
    found = match([lines[0] for lines in receipts.values()], payments, receipt_days)
    sources = _evidence(found, receipts)
    txns: list[dict[str, object]] = []
    refused = 0
    for key, lines in receipts.items():
        if key in found:
            continue
        first, copies = lines[0], lines[1:]
        if first.currency not in INTERNAL_ACCOUNT.currencies:
            refused += 1
            continue
        txns.append(_paid_outside(first, first.posting_date))
        sources.append(_source(key, first, "primary"))
        sources.extend(_source(key, each, "evidence") for each in copies)
        payments.append(
            Payment(
                id=key,
                account_id=account_id(INTERNAL_ACCOUNT.slug),
                dates=(first.posting_date,),
                posting_date=first.posting_date,
                amount=first.amount,
                currency=first.currency,
                texts=(first.description_norm, first.payee),
            )
        )

    receipt_txns = len(txns)

    invoices = by_kind["invoice"]
    paid = match([lines[0] for lines in invoices.values()], payments, invoice_days)
    decided = _invoice_decisions(uow, invoices)
    paid |= _matched_on_decided_date(invoices, decided, paid, payments, invoice_days)
    for key, txn_id in paid.items():
        first, *copies = invoices[key]
        over = decided.get(key)
        sources.append(
            _source(txn_id, first, "evidence")
            | {"overrides_decision_id": None if over is None else over.id}
        )
        sources.extend(_source(txn_id, each, "evidence") for each in copies)
    still_open = 0
    invoices_refused: list[dict[str, uuid.UUID]] = []
    unpaid: list[UnpaidInvoice] = []
    for key, lines in invoices.items():
        if key in paid:
            continue
        each = decided.get(key)
        if each is None:
            still_open += 1
            continue
        first, *copies = lines
        if first.currency not in INTERNAL_ACCOUNT.currencies:
            invoices_refused.append({"b_id": first.id, "b_decision": each.id})
            still_open += 1
        elif each.kind is DecisionKind.INVOICE_UNPAID:
            unpaid.append(_unpaid(first, each))
        else:
            txns.append(_paid_outside(first, _decided_date(each)))
            sources.append(_source(key, first, "primary"))
            sources.extend(_source(key, copy, "evidence") for copy in copies)

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
    if invoices_refused:
        uow.execute(
            update(document_line)
            .where(document_line.c.id == bindparam("b_id"))
            .values(refused_decision_id=bindparam("b_decision")),
            invoices_refused,
        )
    return Evidence(
        links=sum(1 for each in sources if each["role"] == "evidence"),
        receipt_txns=receipt_txns,
        receipts_currency_refused=refused,
        unmatched_invoices=still_open,
        invoice_txns=len(txns) - receipt_txns,
        invoices_currency_refused=len(invoices_refused),
        decisions_superseded=sum(1 for key in paid if key in decided),
        unpaid_invoices=tuple(unpaid),
        incomplete_lines=incomplete,
    )
