"""The Vancity chequing transaction export: a headerless CSV (FR-3, FR-8).

Each row has seven columns: the full account identifier, the date (``06-Jan-2021``),
the description, a column the exports leave empty, the withdrawal, the deposit and the
running balance. The export may list rows newest first; lines are kept oldest first.

- The account identifier is restricted. It is matched in full against the registry's
  ``number`` identifiers at ``INSTITUTION`` (``AccountLookup.by_identifier``), and goes
  no further: never onto a line, a counterparty or a message. An identifier the
  registry does not hold rejects the file (``unknown_account``); one that two accounts
  hold rejects it as ``ambiguous_account``.
- Each account in the file gives one CAD ``statement_period`` from its first line's
  date to its last's, control kind ``running_balance``. The file's own date range
  proves nothing: an export can be a filtered selection of rows.
- Control is the running balance (``base.balance_runs``): each continuous run is a
  verified segment, and a break between two lines means rows are missing there, so
  both lines load and the span between them is returned as a gap. A period is tied out
  when its lines form one segment with no gap.
- A row holding both a withdrawal and a deposit becomes two lines, the withdrawal
  first, carrying the balance between them (the printed balance less the deposit).

A file that is not this layout (another column count, a header row, a date or an amount
that does not read, a row moving no money, rows out of date order) is ``NotRecognised``.
"""

from __future__ import annotations

import csv
import datetime as dt
import io
from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Final

from rheo_finance.parsers.base import (
    AccountLookup,
    BalanceGap,
    NotRecognised,
    ParsedLine,
    ParsedPeriod,
    ParseResult,
    RegisteredAccount,
    Rejected,
    VerifiedSegment,
    balance_runs,
)
from rheo_finance.parsers.registry import Parser, register_parser

INSTITUTION: Final = "Vancity"
CURRENCY: Final = "CAD"
COLUMNS: Final = 7
_MONTHS: Final = {
    name: number
    for number, name in enumerate(
        ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
         "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"),
        start=1,
    )
}  # fmt: skip


@dataclass(frozen=True)
class _Row:
    date: dt.date
    description: str
    withdrawal: Decimal | None
    deposit: Decimal | None
    balance: Decimal


def _day(cell: str) -> dt.date:
    try:
        day, month, year = cell.strip().split("-")
        return dt.date(int(year), _MONTHS[month.title()], int(day))
    except (KeyError, ValueError):
        raise NotRecognised("not a Vancity chequing export") from None


def _amount(cell: str) -> Decimal | None:
    if not cell.strip():
        return None
    try:
        value = Decimal(cell.strip())
    except InvalidOperation:
        raise NotRecognised("not a Vancity chequing export") from None
    if not value.is_finite():
        raise NotRecognised("not a Vancity chequing export")
    return value


def _rows(text: str) -> dict[str, list[_Row]]:
    """The rows by account identifier, in file order."""
    by_account: dict[str, list[_Row]] = defaultdict(list)
    for cells in csv.reader(io.StringIO(text)):
        if not any(cell.strip() for cell in cells):
            continue
        if len(cells) != COLUMNS or not cells[0].strip():
            raise NotRecognised("not a Vancity chequing export")
        withdrawal, deposit = _amount(cells[4]), _amount(cells[5])
        balance = _amount(cells[6])
        if balance is None or (withdrawal is None and deposit is None):
            raise NotRecognised("not a Vancity chequing export")
        by_account[cells[0].strip()].append(
            _Row(_day(cells[1]), cells[2].strip(), withdrawal, deposit, balance)
        )
    if not by_account:
        raise NotRecognised("not a Vancity chequing export")
    return by_account


Triple = tuple[dt.date, Decimal, Decimal]


def _lines(
    registered: RegisteredAccount, rows: list[_Row]
) -> tuple[list[ParsedLine], list[Triple]]:
    """The lines, with each one's (date, amount, balance) for ``balance_runs``."""
    lines: list[ParsedLine] = []
    triples: list[Triple] = []

    def add(row: _Row, amount: Decimal, balance: Decimal) -> None:
        lines.append(
            ParsedLine(
                "transaction",
                account=registered,
                posting_date=row.date,
                amount=amount,
                currency=CURRENCY,
                description_raw=row.description,
                running_balance=balance,
            )
        )
        triples.append((row.date, amount, balance))

    for row in rows:
        if row.withdrawal is not None and row.deposit is not None:
            add(row, -row.withdrawal, row.balance - row.deposit)
            add(row, row.deposit, row.balance)
        elif row.withdrawal is not None:
            add(row, -row.withdrawal, row.balance)
        elif row.deposit is not None:
            add(row, row.deposit, row.balance)
    return lines, triples


def _oldest_first(registered: RegisteredAccount, rows: list[_Row]) -> list[_Row]:
    """The rows in posting order. A file in neither date order is not this layout;
    one whose rows all share a date keeps the order its balances continue in."""
    dates = [row.date for row in rows]
    orders: list[list[_Row]] = []
    if dates == sorted(dates):
        orders.append(rows)
    if dates == sorted(dates, reverse=True):
        orders.append(rows[::-1])
    if not orders:
        raise NotRecognised("not a Vancity chequing export")

    def breaks(order: list[_Row]) -> int:
        return len(balance_runs(registered, _lines(registered, order)[1])[1])

    return min(orders, key=breaks)


def parse(text: str, accounts: AccountLookup) -> ParseResult:
    by_account = _rows(text)
    periods: list[ParsedPeriod] = []
    lines: list[ParsedLine] = []
    segments: list[VerifiedSegment] = []
    gaps: list[BalanceGap] = []
    seen: set[RegisteredAccount] = set()
    for identifier, rows in by_account.items():
        dates = [row.date for row in rows]
        period = (min(dates), max(dates))
        found = accounts.by_identifier(INSTITUTION, identifier, *period)
        if not found:
            raise Rejected("unknown_account", period)
        if len(found) > 1 or found[0] in seen:
            raise Rejected("ambiguous_account", period)
        registered = found[0]
        seen.add(registered)
        account_lines, triples = _lines(registered, _oldest_first(registered, rows))
        account_segments, account_gaps = balance_runs(registered, triples)
        periods.append(
            ParsedPeriod(
                account=registered,
                currency=CURRENCY,
                period_start=period[0],
                period_end=period[1],
                control_kind="running_balance",
                tied_out=len(account_segments) == 1 and not account_gaps,
            )
        )
        lines += account_lines
        segments += account_segments
        gaps += account_gaps
    return ParseResult(
        periods=tuple(periods),
        lines=tuple(lines),
        control_kind="running_balance",
        segments=tuple(segments),
        gaps=tuple(gaps),
    )


def filing_date(text: str) -> dt.date:
    """The last row's date, the date the filer names a new copy by."""
    return max(row.date for rows in _rows(text).values() for row in rows)


PARSER: Final = register_parser(
    Parser(
        parser_id="vancity.chequing_csv",
        issuer="vancity",
        format="csv",
        version="1",
        parse=parse,
        filing_date=filing_date,
    )
)
