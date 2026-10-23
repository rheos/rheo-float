"""The Vancity Visa transaction export: a headerless CSV, control ``none`` (FR-3, FR-8).

Each row has eight columns: the cardholder's name, the masked card number
(``9999 99•• •••• 9999``), the transaction date (``2024-01-31``), the posting date and
time (``2024-02-01T00:00``), the description, the currency, the debit and the credit.
Exactly one of the last two holds an amount. The export lists rows newest first by
transaction date; lines are kept oldest first.

- The name and the masked card are restricted. The name is read past and stored
  nowhere: not on a line, not as a counterparty, not in a message. The card is matched
  by its last four at ``INSTITUTION`` (``parsers.card.card_account``) and goes no
  further. A card the registry does not hold rejects the file (``unknown_account``).
- Each account gives one CAD ``statement_period`` from its earliest posting date to
  its latest, control kind ``none``: the export has no totals and no balance, and its
  date range proves nothing about completeness.
- A debit is negative, a credit positive (``parsers.card``'s sign convention).

A file that is not this layout (another column count, a date or an amount that does
not read, a row with both or neither amount, a currency other than CAD, rows in
neither transaction-date order, a CSV the reader cannot read) is ``NotRecognised``.
"""

from __future__ import annotations

import csv
import datetime as dt
import io
import re
from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal
from typing import Final

from rheo_finance.parsers.base import (
    AccountLookup,
    NotRecognised,
    ParsedLine,
    ParsedPeriod,
    ParseResult,
    RegisteredAccount,
)
from rheo_finance.parsers.card import (
    ACCOUNT_KIND,
    CURRENCY,
    amount,
    card_account,
    written_date,
)
from rheo_finance.parsers.hints import with_hints
from rheo_finance.parsers.registry import Parser, register_parser

ISSUER: Final = "vancity-visa"
INSTITUTION: Final = "Vancity"
COLUMNS: Final = 8
_CARD: Final = re.compile(r"\d{4} \d\d•• •••• (\d{4})")
_POSTED: Final = re.compile(r"(\d{4}-\d\d-\d\d)T\d\d:\d\d")


@dataclass(frozen=True)
class _Row:
    four: str
    txn_date: dt.date
    posting_date: dt.date
    description: str
    amount: Decimal


def _row(cells: list[str]) -> _Row:
    card = _CARD.fullmatch(cells[1].strip())
    posted = _POSTED.fullmatch(cells[3].strip())
    if card is None or posted is None or cells[5].strip() != CURRENCY:
        raise NotRecognised("not a Vancity Visa export")
    debit, credit = cells[6].strip(), cells[7].strip()
    if bool(debit) == bool(credit):
        raise NotRecognised("not a Vancity Visa export")
    value = amount(credit) if credit else -amount(debit)
    return _Row(
        four=card.group(1),
        txn_date=written_date(cells[2], "%Y-%m-%d"),
        posting_date=written_date(posted.group(1), "%Y-%m-%d"),
        description=" ".join(cells[4].split()),
        amount=value,
    )


def _rows(text: str) -> list[_Row]:
    try:
        # A cell over the reader's size limit raises csv.Error, not ValueError.
        found = list(csv.reader(io.StringIO(text)))
    except csv.Error:
        raise NotRecognised("not a Vancity Visa export") from None
    rows: list[_Row] = []
    for cells in found:
        if not any(cell.strip() for cell in cells):
            continue
        if len(cells) != COLUMNS:
            raise NotRecognised("not a Vancity Visa export")
        rows.append(_row(cells))
    if not rows:
        raise NotRecognised("not a Vancity Visa export")
    dates = [row.txn_date for row in rows]
    if dates == sorted(dates, reverse=True):
        return rows[::-1]
    if dates == sorted(dates):
        return rows
    raise NotRecognised("not a Vancity Visa export")


def _span(rows: list[_Row]) -> tuple[dt.date, dt.date]:
    dates = [row.posting_date for row in rows]
    return min(dates), max(dates)


def parse(text: str, accounts: AccountLookup) -> ParseResult:
    rows = _rows(text)
    by_four: dict[str, list[_Row]] = defaultdict(list)
    for row in rows:
        by_four[row.four].append(row)
    by_account: dict[RegisteredAccount, list[_Row]] = defaultdict(list)
    for four, own in by_four.items():
        by_account[card_account(accounts, INSTITUTION, four, _span(own))] += own
    periods: list[ParsedPeriod] = []
    lines: list[ParsedLine] = []
    for registered, own in by_account.items():
        own.sort(key=lambda row: row.txn_date)
        start, end = _span(own)
        periods.append(
            ParsedPeriod(
                account=registered,
                currency=CURRENCY,
                period_start=start,
                period_end=end,
                control_kind="none",
            )
        )
        lines += [
            with_hints(
                ParsedLine(
                    "transaction",
                    account=registered,
                    posting_date=row.posting_date,
                    txn_date=row.txn_date,
                    amount=row.amount,
                    currency=CURRENCY,
                    description_raw=row.description,
                ),
                account_kind=ACCOUNT_KIND,
                issuer=ISSUER,
            )
            for row in own
        ]
    return ParseResult(periods=tuple(periods), lines=tuple(lines), control_kind="none")


def filing_date(text: str) -> dt.date:
    """The latest posting date, the date the filer names a new copy by."""
    return _span(_rows(text))[1]


PARSER: Final = register_parser(
    Parser(
        parser_id="vancity_visa.csv",
        issuer=ISSUER,
        format="csv",
        version="1",
        parse=parse,
        filing_date=filing_date,
        sets_hints=True,
    )
)
