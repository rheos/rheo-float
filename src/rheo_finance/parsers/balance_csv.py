"""What the fiat CSV exports with a balance column share (FR-12(a), FR-3
``running_balance``): an exact header, signed amounts, and lines checked per currency
against the balance each row prints.

- ``rows`` reads the file as a table whose header must be exactly the parser's
  columns (cells compared without surrounding spaces); any other header, a row of
  another width, or a CSV the reader cannot read is ``NotRecognised``.
- ``Movement`` is one row as the lines it gives (a principal line and, when the row
  states a fee, a fee line) with the balance the row ends on, fee included.
- ``running_balance_result`` puts each currency's movements oldest first, gives every
  line the balance after it, and splits the lines into verified segments and gaps
  (``base.balance_runs``). Each currency is one ``statement_period`` from its first
  line's date to its last's, tied out when its lines form one segment with no gap. The
  file's own date range proves nothing: an export can be a filtered selection.
"""

from __future__ import annotations

import csv
import dataclasses
import datetime as dt
import io
import re
from collections import defaultdict
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from decimal import Decimal
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

_SIGNED: Final = re.compile(r"-?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?")
_CURRENCY: Final = re.compile(r"[A-Z]{3}")


def rows(text: str, columns: tuple[str, ...], what: str) -> list[dict[str, str]]:
    """The file's rows by column name, blank rows skipped."""
    try:
        # A cell over the reader's size limit raises csv.Error, not ValueError.
        found = [
            cells
            for cells in csv.reader(io.StringIO(text))
            if any(cell.strip() for cell in cells)
        ]
    except csv.Error:
        raise NotRecognised(what) from None
    if not found or tuple(cell.strip() for cell in found[0]) != columns:
        raise NotRecognised(what)
    body = found[1:]
    if any(len(cells) != len(columns) for cells in body):
        raise NotRecognised(what)
    return [dict(zip(columns, cells, strict=True)) for cells in body]


def signed(cell: str, what: str) -> Decimal:
    """A printed signed amount (``-1,234.56``) as an exact Decimal."""
    text = cell.strip()
    if _SIGNED.fullmatch(text) is None:
        raise NotRecognised(what)
    return Decimal(text.replace(",", ""))


def currency(cell: str, what: str) -> str:
    text = cell.strip()
    if _CURRENCY.fullmatch(text) is None:
        raise NotRecognised(what)
    return text


@dataclass(frozen=True)
class Movement:
    """One row: its date, currency, the balance it ends on, and its lines on an
    account in order (their amounts sum to the row's change of balance)."""

    day: dt.date
    currency: str
    balance: Decimal
    lines: Callable[[RegisteredAccount], tuple[ParsedLine, ...]]


def span(movements: Sequence[Movement]) -> tuple[dt.date, dt.date]:
    days = [each.day for each in movements]
    return min(days), max(days)


def one_account(
    accounts: AccountLookup, institution: str, movements: Sequence[Movement]
) -> RegisteredAccount:
    """The one account registered at ``institution`` over the file's dates, for an
    export that prints no account identifier; ``unknown_account`` or
    ``ambiguous_account`` otherwise."""
    period = span(movements)
    found = accounts.by_institution(institution, *period)
    if not found:
        raise Rejected("unknown_account", period)
    if len(found) > 1:
        raise Rejected("ambiguous_account", period)
    return found[0]


Triple = tuple[dt.date, Decimal, Decimal]


def _balanced(
    registered: RegisteredAccount, movements: Sequence[Movement]
) -> tuple[list[ParsedLine], list[Triple]]:
    """The lines with the balance after each, and their (date, amount, balance)."""
    lines: list[ParsedLine] = []
    triples: list[Triple] = []
    for movement in movements:
        own = movement.lines(registered)
        balance = movement.balance - sum(
            (line.amount or Decimal(0) for line in own), Decimal(0)
        )
        for line in own:
            balance += line.amount or Decimal(0)
            lines.append(dataclasses.replace(line, running_balance=balance))
            triples.append((movement.day, line.amount or Decimal(0), balance))
    return lines, triples


def _oldest_first(
    registered: RegisteredAccount, movements: list[Movement], what: str
) -> list[Movement]:
    """The movements in posting order. A file in neither date order is not the
    layout; one whose rows all share a date keeps the order its balances continue in."""
    days = [each.day for each in movements]
    orders: list[list[Movement]] = []
    if days == sorted(days):
        orders.append(movements)
    if days == sorted(days, reverse=True):
        orders.append(movements[::-1])
    if not orders:
        raise NotRecognised(what)

    def breaks(order: list[Movement]) -> int:
        return len(balance_runs(registered, _balanced(registered, order)[1])[1])

    return min(orders, key=breaks)


def running_balance_result(
    registered: RegisteredAccount, movements: Sequence[Movement], what: str
) -> ParseResult:
    by_currency: dict[str, list[Movement]] = defaultdict(list)
    for movement in movements:
        by_currency[movement.currency].append(movement)
    periods: list[ParsedPeriod] = []
    lines: list[ParsedLine] = []
    segments: list[VerifiedSegment] = []
    gaps: list[BalanceGap] = []
    for code in sorted(by_currency):
        ordered = _oldest_first(registered, by_currency[code], what)
        own, triples = _balanced(registered, ordered)
        own_segments, own_gaps = balance_runs(registered, triples)
        start, end = span(ordered)
        periods.append(
            ParsedPeriod(
                account=registered,
                currency=code,
                period_start=start,
                period_end=end,
                control_kind="running_balance",
                tied_out=len(own_segments) == 1 and not own_gaps,
            )
        )
        lines += own
        segments += own_segments
        gaps += own_gaps
    return ParseResult(
        periods=tuple(periods),
        lines=tuple(lines),
        control_kind="running_balance",
        segments=tuple(segments),
        gaps=tuple(gaps),
    )
