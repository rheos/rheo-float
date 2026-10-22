"""Derivation stage 3, part one: every line of a parsed document becomes a txn, and so
does every manual cash line (FR-4, FR-13, Key decisions 3 and 5).

**Natural key.** A fiat document line is keyed by (account slug, posting date, signed
amount, currency, normalized description, occurrence), the occurrence counting the
identical lines before it on the same account and day, so two identical same-day lines
both survive. A manual cash line has its own key space (a leading ``manual``), counted
among manual lines only, so a statement that later prints the same line neither takes
its id nor shifts it. The txn id and the line's ``line_key_id`` are ``derived_id`` of
the key on the posting date; a rebuild reproduces them, and a decision names a line by
``finance.txn:<id>``. Each txn has one ``primary`` ``txn_source`` carrying that key.

**What becomes a txn.** Lines of kind ``transaction`` with a posting date, an amount and
a currency, from documents with status ``parsed``; a transaction line missing one of the
three is left out and counted (``Lines.incomplete_lines``). Other line kinds belong to
stages and features that are not built yet (invoices, receipts, crypto lines, trading
results, holdings, notices) and are left alone.

**Shortcut.** Documents are read in sha256 order and every line of every document
becomes a txn: a line an overlapping document repeats is a second txn (counted as a
further occurrence). Ranking and the one-to-one overlap matcher replace this in the
next prompt, which makes the first document's lines the txns and later documents'
lines additional sources.

**Manual lines.** The currency of a line must be one its account takes
(``account.currencies``). A line in another currency is not loaded: it is reported as a
``manual_entry_error`` row on its ``transactions.csv`` line, like every other bad manual
row, and counted in the result.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections import Counter
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import bindparam, insert, select, update

from rheo_finance.context import UnitOfWork
from rheo_finance.manual.cash_lines import TRANSACTIONS_FILE, CashLines
from rheo_finance.manual.loader import RowError
from rheo_finance.manual.stage2 import account_id, error_id
from rheo_finance.storage.ids import derived_id
from rheo_finance.storage.schema import (
    account,
    document,
    document_line,
    manual_entry_error,
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
    from_manual: int = 0
    # Manual lines left out because their currency is not one the account takes.
    manual_currency_refused: int = 0
    # Transaction lines of parsed documents left out because they lack a posting date,
    # an amount or a currency. A parser should never store one; if one does, it is
    # counted here rather than lost without a trace.
    incomplete_lines: int = 0

    @property
    def txns(self) -> int:
        return self.from_documents + self.from_manual


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


def _document_lines(uow: UnitOfWork) -> Lines:
    rows = uow.execute(
        select(
            document_line.c.id,
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
    occurrences = _Occurrences()
    txns: list[dict[str, object]] = []
    sources: list[dict[str, object]] = []
    updates: list[dict[str, object]] = []
    incomplete = 0
    for row in rows:
        if row.posting_date is None or row.amount is None or row.currency is None:
            incomplete += 1
            continue
        norm = normalize_description(row.description_raw or "")
        key = (DOCUMENT_KEY, row.slug, row.posting_date, row.amount, row.currency, norm)
        line_id = line_key(*key, occurrences.next(key))
        txns.append(
            {
                "id": line_id,
                "account_id": row.account_id,
                "posting_date": row.posting_date,
                "txn_date": row.txn_date,
                "occurred_at": row.occurred_at,
                "amount": row.amount,
                "currency": row.currency,
                "description_norm": norm,
                "counterparty": row.counterparty,
                "txid": row.txid,
                "from_address": row.from_address,
                "to_address": row.to_address,
                "origin": "document_line",
                "movement_kind": "fiat",
                "fee_type": row.fee_type,
                "trade_ref": row.trade_ref,
            }
        )
        sources.append(
            {
                "txn_id": line_id,
                "document_line_id": row.id,
                "manual_ref": None,
                "line_key_id": line_id,
                "role": "primary",
            }
        )
        updates.append({"b_id": row.id, "b_norm": norm, "b_key": line_id})
    if txns:
        uow.execute(insert(txn), txns)
        uow.execute(insert(txn_source), sources)
        uow.execute(
            update(document_line)
            .where(document_line.c.id == bindparam("b_id"))
            .values(
                description_norm=bindparam("b_norm"), line_key_id=bindparam("b_key")
            ),
            updates,
        )
    return Lines(from_documents=len(txns), incomplete_lines=incomplete)


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
    uow: UnitOfWork, cash: CashLines, account_currencies: dict[str, set[str]]
) -> Lines:
    """Write the txns and their primary sources. ``account_currencies`` maps each
    loaded account's slug to the currencies it takes."""
    documents = _document_lines(uow)
    manual = _manual_lines(uow, cash, account_currencies)
    return Lines(
        from_documents=documents.from_documents,
        incomplete_lines=documents.incomplete_lines,
        from_manual=manual.from_manual,
        manual_currency_refused=manual.manual_currency_refused,
    )
