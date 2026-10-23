"""Derivation stage 3, part one: the lines of parsed documents and the manual cash lines
become txns (FR-4, FR-13, Key decisions 3, 4 and 5).

**Natural key.** A fiat document line is keyed by (account slug, posting date, signed
amount, currency, normalized description, occurrence), the occurrence counting the
identical lines before it in the same document, so two identical same-day lines both
survive and a document's keys depend on no other document. A manual cash line has its
own key space (a leading ``manual``), counted among manual lines only, so a statement
that later prints the same line neither takes its id nor shifts it. A txn's id and its
primary source's ``line_key_id`` are ``derived_id`` of the key on the posting date; an
additional source keeps its own line's key, so a reference to a txn that a better
source later replaced still resolves. Every keyed line stores its key and normalized
description back on ``document_line``.

**What becomes a txn.** Lines of kind ``transaction`` with a posting date, an amount and
a currency, from documents with status ``parsed``, decided by ``overlap.match_lines``
over the documents in ``precedence`` order: a txn (with its basis and provisional
flag), an additional source of a txn, or a ``source_discrepancy`` row. A transaction
line missing one of the three is left out and counted (``Lines.incomplete_lines``).
Receipt and invoice lines are part two (``evidence``), matched against every txn part
one wrote, the manual lines included. Other line kinds belong to stages and features
that are not built yet (crypto lines, trading results, holdings, notices) and are left
alone.

**Manual lines.** The currency of a line must be one its account takes
(``account.currencies``). A line in another currency is not loaded: it is reported as a
``manual_entry_error`` row on its ``transactions.csv`` line, like every other bad manual
row, and counted in the result.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections import Counter, defaultdict
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from sqlalchemy import Row, bindparam, insert, select, update

from rheo_finance.context import UnitOfWork
from rheo_finance.derive import evidence, overlap
from rheo_finance.derive.precedence import standings
from rheo_finance.manual.cash_lines import TRANSACTIONS_FILE, CashLines
from rheo_finance.manual.loader import RowError
from rheo_finance.manual.stage2 import account_id, error_id
from rheo_finance.storage.ids import derived_id
from rheo_finance.storage.schema import (
    account,
    document,
    document_line,
    manual_entry_error,
    source_discrepancy,
    txn,
    txn_source,
)
from rheo_finance.text import normalize_description

CURRENCY_REFUSED = "currency: the account does not take this currency"
DOCUMENT_KEY = "fiat"
MANUAL_KEY = "manual"

Key = tuple[str, str, dt.date, Decimal, str, str]


@dataclass(frozen=True)
class Lines:
    """What stage 3 wrote, as counts."""

    from_documents: int = 0
    # Lines of later documents that matched a txn: sources, not txns.
    additional_sources: int = 0
    # Lines that are neither a txn nor a source (``source_discrepancy`` rows).
    discrepancies: int = 0
    from_manual: int = 0
    # Manual lines left out because their currency is not one the account takes.
    manual_currency_refused: int = 0
    # Transaction, receipt and invoice lines of parsed documents left out because they
    # lack a posting date, an amount or a currency. A parser should never store one;
    # if one does, it is counted here rather than lost without a trace.
    incomplete_lines: int = 0
    # Receipt and invoice lines that are ``evidence`` sources (part two).
    evidence_links: int = 0
    # Unmatched receipts, each a txn on "Paid outside recorded accounts".
    from_receipts: int = 0
    # Invoices no payment line matched: no txn.
    unmatched_invoices: int = 0

    @property
    def txns(self) -> int:
        return self.from_documents + self.from_manual + self.from_receipts


class _Occurrences:
    """The occurrence number of each line among identical ones, in the order given."""

    def __init__(self) -> None:
        self._seen: Counter[Key] = Counter()

    def next(self, key: Key) -> int:
        self._seen[key] += 1
        return self._seen[key]


def line_key(
    kind: str,
    slug: str,
    posting_date: dt.date,
    amount: Decimal,
    currency: str,
    description_norm: str,
    occurrence: int,
) -> uuid.UUID:
    """The id of a fiat line (also the id of its txn)."""
    return derived_id(
        posting_date,
        (kind, slug, posting_date, amount, currency, description_norm, occurrence),
    )


def _document_lines(uow: UnitOfWork, overlap_days: int) -> Lines:
    rows = uow.execute(
        select(
            document_line.c.id,
            document_line.c.document_id,
            document_line.c.account_id,
            account.c.slug,
            document_line.c.posting_date,
            document_line.c.txn_date,
            document_line.c.occurred_at,
            document_line.c.amount,
            document_line.c.currency,
            document_line.c.description_raw,
            document_line.c.counterparty,
            document_line.c.txid,
            document_line.c.from_address,
            document_line.c.to_address,
            document_line.c.fee_type,
            document_line.c.trade_ref,
        )
        .join(document, document.c.id == document_line.c.document_id)
        .join(account, account.c.id == document_line.c.account_id)
        .where(
            document.c.status == "parsed",
            document_line.c.line_kind == "transaction",
        )
        .order_by(document.c.sha256, document_line.c.line_no)
    ).all()
    # Occurrences count identical lines within one document: a document's keys do not
    # depend on any other document.
    occurrences: dict[uuid.UUID, _Occurrences] = defaultdict(_Occurrences)
    lines: dict[tuple[uuid.UUID, uuid.UUID], list[overlap.Line]] = defaultdict(list)
    extra: dict[uuid.UUID, Row[Any]] = {}
    incomplete = 0
    for row in rows:
        if row.posting_date is None or row.amount is None or row.currency is None:
            incomplete += 1
            continue
        norm = normalize_description(row.description_raw or "")
        key = (DOCUMENT_KEY, row.slug, row.posting_date, row.amount, row.currency, norm)
        line = overlap.Line(
            id=row.id,
            document_id=row.document_id,
            account_id=row.account_id,
            posting_date=row.posting_date,
            txn_date=row.txn_date,
            amount=row.amount,
            currency=row.currency,
            description_norm=norm,
            key=line_key(*key, occurrences[row.document_id].next(key)),
        )
        lines[row.account_id, row.document_id].append(line)
        extra[row.id] = row
    outcome = overlap.match_lines(standings(uow), lines, overlap_days)

    txns: list[dict[str, object]] = []
    sources: list[dict[str, object]] = []
    for commit in outcome.commits:
        line, row = commit.line, extra[commit.line.id]
        txns.append(
            {
                "id": line.key,
                "account_id": line.account_id,
                "posting_date": line.posting_date,
                "txn_date": line.txn_date,
                "occurred_at": row.occurred_at,
                "amount": line.amount,
                "currency": line.currency,
                "description_norm": line.description_norm,
                "counterparty": row.counterparty,
                "txid": row.txid,
                "from_address": row.from_address,
                "to_address": row.to_address,
                "origin": "document_line",
                "basis": commit.basis,
                "provisional": commit.provisional,
                "movement_kind": "fiat",
                "fee_type": row.fee_type,
                "trade_ref": row.trade_ref,
            }
        )
        sources.append(_source(line.key, line, "primary"))
    sources.extend(
        _source(join.txn_id, join.line, "additional") for join in outcome.joins
    )
    if txns:
        uow.execute(insert(txn), txns)
    if sources:
        uow.execute(insert(txn_source), sources)
    if outcome.discrepancies:
        uow.execute(
            insert(source_discrepancy),
            [
                {
                    "id": derived_id(
                        each.line.posting_date, ("source_discrepancy", each.line.id)
                    ),
                    "account_id": each.line.account_id,
                    "document_id": each.line.document_id,
                    "document_line_id": each.line.id,
                    "kind": each.kind,
                }
                for each in outcome.discrepancies
            ],
        )
    keyed = [line for found in lines.values() for line in found]
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
    return Lines(
        from_documents=len(txns),
        additional_sources=len(outcome.joins),
        discrepancies=len(outcome.discrepancies),
        incomplete_lines=incomplete,
    )


def _source(txn_id: uuid.UUID, line: overlap.Line, role: str) -> dict[str, object]:
    return {
        "txn_id": txn_id,
        "document_line_id": line.id,
        "manual_ref": None,
        "line_key_id": line.key,
        "role": role,
    }


def _manual_lines(
    uow: UnitOfWork, cash: CashLines, currencies: dict[str, set[str]]
) -> Lines:
    occurrences = _Occurrences()
    txns: list[dict[str, object]] = []
    sources: list[dict[str, object]] = []
    refused: list[RowError] = []
    for line in cash.lines:
        row = line.row
        if row.currency not in currencies[row.account]:
            refused.append(RowError(TRANSACTIONS_FILE, line.line_no, CURRENCY_REFUSED))
            continue
        norm = normalize_description(row.description)
        key = (MANUAL_KEY, row.account, row.date, row.amount, row.currency, norm)
        line_id = line_key(*key, occurrences.next(key))
        txns.append(
            {
                "id": line_id,
                "account_id": account_id(row.account),
                "posting_date": row.date,
                "amount": row.amount,
                "currency": row.currency,
                "description_norm": norm,
                "origin": "manual",
                "basis": "manual",
                "movement_kind": "fiat",
            }
        )
        sources.append(
            {
                "txn_id": line_id,
                "document_line_id": None,
                "manual_ref": line.source_ref,
                "line_key_id": line_id,
                "role": "primary",
            }
        )
    if txns:
        uow.execute(insert(txn), txns)
        uow.execute(insert(txn_source), sources)
    if refused:
        uow.execute(
            insert(manual_entry_error),
            [
                {
                    "id": error_id(error),
                    "file": error.file,
                    "line_no": error.line_no,
                    "reason": error.reason,
                }
                for error in refused
            ],
        )
    return Lines(from_manual=len(txns), manual_currency_refused=len(refused))


def build_lines(
    uow: UnitOfWork,
    cash: CashLines,
    account_currencies: dict[str, set[str]],
    *,
    overlap_days: int,
    receipt_days: int,
    invoice_days: int,
) -> Lines:
    """Write the txns, their sources and the source discrepancies, then link the
    receipts and invoices (``evidence``). ``account_currencies`` maps each loaded
    account's slug to the currencies it takes; ``overlap_days`` is
    ``finance.import.overlap_day_tolerance``, ``receipt_days`` and ``invoice_days``
    the receipt and invoice matching windows."""
    documents = _document_lines(uow, overlap_days)
    manual = _manual_lines(uow, cash, account_currencies)
    linked = evidence.link(uow, receipt_days=receipt_days, invoice_days=invoice_days)
    return Lines(
        from_documents=documents.from_documents,
        additional_sources=documents.additional_sources,
        discrepancies=documents.discrepancies,
        incomplete_lines=documents.incomplete_lines + linked.incomplete_lines,
        from_manual=manual.from_manual,
        manual_currency_refused=manual.manual_currency_refused,
        evidence_links=linked.links,
        from_receipts=linked.receipt_txns,
        unmatched_invoices=linked.unmatched_invoices,
    )
