"""The MBNA monthly statement (PDF layout text), control ``summary`` (FR-3, FR-8,
FR-19, FR-20).

The layout (what ``pdf_text`` prints):

- ``Statement Period: MM/DD/YY to MM/DD/YY`` and ``Statement Closing Date <Month>
  <day>, <year>``, which is the period's end and the statement date. The masked card
  prints as ``9999 99XX XXXX 9999``, beside the cardholder's name, which is read past.
- The summary: ``Previous Statement Balance``, ``Payments``, ``Other Credits`` (when
  printed), ``New Purchases``, ``Balance Transfers and Access Cheques``, ``Cash
  Advances``, ``Interest``, ``Fees`` and ``Your New Balance``, a credit printed
  ``-$<amount>``. The payment information prints ``Your Minimum Payment`` and ``Your
  Minimum Payment Due Date``; a statement may print no minimum.
- Transactions, under section headings (``PAYMENTS``, ``PURCHASES``, ``INTEREST
  CHARGED`` and others): ``MM/DD/YY   MM/DD/YY   <description>   <reference>
  $<amount>``, the transaction date then the posting date, a four-digit reference
  number where the line has one, a credit printed ``-$<amount>``. The section totals,
  the previous and new balance lines and every other line are read past.

One CAD ``statement_period`` per statement, signed and filled as ``parsers.card``
says. ``total_payments`` is the printed ``Payments`` figure. The registry ties the
balances out to the cent. The debit and credit totals are left unset: the summary nets
a refund printed among the purchases into ``New Purchases``, so its categories are
not the sums of the lines by sign.

Text that is not this layout is ``NotRecognised``.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass
from decimal import Decimal
from typing import Final

from rheo_finance.parsers.base import (
    AccountLookup,
    NotRecognised,
    ParsedLine,
    ParsedPeriod,
    ParseResult,
)
from rheo_finance.parsers.card import (
    ACCOUNT_KIND,
    AMOUNT_TEXT,
    CURRENCY,
    amount,
    card_account,
    one_value,
    written_date,
)
from rheo_finance.parsers.hints import with_hints
from rheo_finance.parsers.registry import Parser, register_parser

ISSUER: Final = "mbna"
INSTITUTION: Final = "MBNA"

_SLASHED: Final = r"\d\d/\d\d/\d\d"
# Every label and its value sit on one line: [ \t] never crosses a line end.
_PERIOD: Final = re.compile(rf"Statement Period:[ \t]+({_SLASHED}) to ({_SLASHED})")
_CLOSING: Final = re.compile(
    r"Statement Closing Date[ \t]+([A-Z][a-z]+ \d{1,2}, \d{4})"
)
_CARD: Final = re.compile(r"\b\d{4} \d\dXX XXXX (\d{4})\b")
_SIGNED: Final = rf"(-)?\$({AMOUNT_TEXT})\b"
_ROW: Final = re.compile(
    rf"^\s*({_SLASHED})\s+({_SLASHED})\s+(.*?)(?:\s+\d{{4}})?\s+(-)?\$"
    rf"({AMOUNT_TEXT})\s*$"
)


@dataclass(frozen=True)
class _Statement:
    start: dt.date
    end: dt.date
    four: str
    previous: Decimal
    balance: Decimal
    payments: Decimal
    minimum: Decimal | None
    due: dt.date


def _slashed(text: str) -> dt.date:
    return written_date(text, "%m/%d/%y")


def _signed(match: re.Match[str]) -> Decimal:
    value = amount(match.group(2))
    return -value if match.group(1) else value


def _labelled(text: str, label: str, *, optional: bool = False) -> Decimal | None:
    pattern = re.compile(rf"^[ \t]*{label}[ \t]+{_SIGNED}", re.MULTILINE)
    values = [_signed(match) for match in pattern.finditer(text)]
    if optional and not values:
        return None
    return one_value(values)


def _period(text: str) -> tuple[dt.date, dt.date]:
    found = [match.groups() for match in _PERIOD.finditer(text)]
    if not found:
        raise NotRecognised("not an MBNA statement")
    first, last = one_value(found)
    start, end = _slashed(first), _slashed(last)
    closing = [
        written_date(match.group(1), "%B %d, %Y") for match in _CLOSING.finditer(text)
    ]
    if start > end or one_value(closing) != end:
        raise NotRecognised("not an MBNA statement")
    return start, end


def _statement(text: str) -> _Statement:
    start, end = _period(text)
    previous = _labelled(text, "Previous Statement Balance")
    balance = _labelled(text, "Your New Balance")
    payments = _labelled(text, "Payments")
    minimum = _labelled(text, r"Your Minimum Payment", optional=True)
    due = re.compile(r"Your Minimum Payment Due Date[ \t]+([A-Z][a-z]+ \d{1,2}, \d{4})")
    if previous is None or balance is None or payments is None:
        raise NotRecognised("not an MBNA statement")
    return _Statement(
        start=start,
        end=end,
        four=one_value([match.group(1) for match in _CARD.finditer(text)]),
        previous=previous,
        balance=balance,
        payments=-payments,
        minimum=minimum,
        due=written_date(
            one_value([match.group(1) for match in due.finditer(text)]), "%B %d, %Y"
        ),
    )


def parse(text: str, accounts: AccountLookup) -> ParseResult:
    statement = _statement(text)
    period = (statement.start, statement.end)
    registered = card_account(accounts, INSTITUTION, statement.four, period)
    lines: list[ParsedLine] = []
    for row in text.splitlines():
        match = _ROW.match(row)
        if match is None:
            continue
        txn_date, posting_date, description, credit, printed = match.groups()
        value = amount(printed)
        line = ParsedLine(
            "transaction",
            account=registered,
            posting_date=_slashed(posting_date),
            txn_date=_slashed(txn_date),
            amount=value if credit else -value,
            currency=CURRENCY,
            description_raw=" ".join(description.split()),
        )
        lines.append(with_hints(line, account_kind=ACCOUNT_KIND, issuer=ISSUER))
    return ParseResult(
        periods=(
            ParsedPeriod(
                account=registered,
                currency=CURRENCY,
                period_start=statement.start,
                period_end=statement.end,
                control_kind="summary",
                opening=-statement.previous,
                closing=-statement.balance,
                total_payments=statement.payments,
                balance_due=statement.balance,
                minimum_due=statement.minimum,
                due_date=statement.due,
            ),
        ),
        lines=tuple(lines),
        control_kind="summary",
    )


def filing_date(text: str) -> dt.date:
    """The statement's closing date, its period end."""
    return _period(text)[1]


PARSER: Final = register_parser(
    Parser(
        parser_id="mbna.statement",
        issuer=ISSUER,
        format="pdf",
        version="1",
        parse=parse,
        filing_date=filing_date,
        sets_hints=True,
    )
)
