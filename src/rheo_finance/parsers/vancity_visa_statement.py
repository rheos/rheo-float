"""The Vancity Visa monthly statement (PDF layout text), control ``summary`` (FR-3,
FR-8, FR-14, FR-19, FR-20).

The layout (what ``pdf_text`` prints):

- ``Statement period: <Month> <day> to <Month> <day>, <year>`` on every page; a period
  crossing the new year prints the year once, at its end. ``Account Number:`` follows
  on the same line with the masked card (``9999 99** **** 9999``).
- The summary box: ``Previous balance``, ``Payments & credits``, ``Purchases &
  debits``, ``Cash advances``, ``Interest``, ``Fees`` and ``Statement balance``, each
  at the start of its line. Beside it the payment box prints ``Statement balance``,
  ``Minimum payment`` and ``Payment due date``; a statement may print no minimum.
- Transactions: ``<MON> <day>   <MON> <day> <description>   $<amount>``, the
  transaction date then the posting date, a credit printed ``- $<amount>``. Text of the
  page's right-hand column can follow on the same line; the first amount is the
  line's. A foreign purchase is followed by ``FOREIGN CURRENCY <amount> <currency> @
  <rate>``: the line's amount stays the CAD amount the issuer states (FR-14), and the
  foreign amount and currency ride along. A cardholder heading (the cardholder's name
  and masked card) is read past: no name is stored.

One CAD ``statement_period`` per statement, signed and filled as ``parsers.card``
says. ``total_debits`` is purchases, cash advances, interest and fees; ``total_credits``
is payments and credits; the registry ties them and the balances out to the cent.
``total_payments`` is the sum of the lines worded as a card payment: the summary box
folds refunds into "Payments & credits", and a refund is not a payment.

Text that is not this layout (no period line, no summary box, a date that does not
read) is ``NotRecognised``.
"""

from __future__ import annotations

import dataclasses
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
    RegisteredAccount,
)
from rheo_finance.parsers.card import (
    ACCOUNT_KIND,
    AMOUNT_TEXT,
    CURRENCY,
    MONTH_ABBREVIATIONS,
    amount,
    card_account,
    day_in_period,
    one_value,
    written_date,
)
from rheo_finance.parsers.hints import with_hints
from rheo_finance.parsers.registry import Parser, register_parser
from rheo_finance.reference.patterns import classify
from rheo_finance.text import normalize_description

ISSUER: Final = "vancity-visa"
INSTITUTION: Final = "Vancity"
CARD_PAYMENT: Final = "card_payment"

_MONTHS: Final = "|".join(MONTH_ABBREVIATIONS)
_PERIOD: Final = re.compile(
    r"Statement period:[ \t]+([A-Z][a-z]+) (\d{1,2})(?:, (\d{4}))? to"
    r" ([A-Z][a-z]+) (\d{1,2}), (\d{4})"
)
_CARD: Final = re.compile(r"Account Number:[ \t]+\d{4} \d\d\*\* \*{4} (\d{4})\b")
# A signed balance: -$1.00, $1.00 or $1.00 CR are all printed somewhere by card issuers.
# Every label and its value sit on one line: [ \t] never crosses a line end.
_BALANCE: Final = rf"[ \t]+(-[ \t]*)?\$({AMOUNT_TEXT})([ \t]?CR\b)?"
_ROW: Final = re.compile(
    rf"^({_MONTHS}) (\d{{1,2}})\s+({_MONTHS}) (\d{{1,2}})\s+(.*?)\s+(-\s*)?\$"
    rf"({AMOUNT_TEXT})(?=\s|$)"
)
_FOREIGN: Final = re.compile(
    r"^\s*FOREIGN CURRENCY ((?:\d{1,3}(?:,\d{3})*|\d+)(?:\.\d+)?) ([A-Z]{3}) @"
    r" \d+(?:\.\d+)?(?=\s|$)"
)
# The summary box's debit and credit categories, each at the start of its line.
_DEBITS: Final = ("Purchases & debits", "Cash advances", "Interest", "Fees")
_CREDITS: Final = ("Payments & credits",)


@dataclass(frozen=True)
class _Statement:
    start: dt.date
    end: dt.date
    four: str
    previous: Decimal
    balance: Decimal
    debits: Decimal
    credits: Decimal
    minimum: Decimal | None
    due: dt.date


def _signed_balance(match: re.Match[str], first: int) -> Decimal:
    """The balance owed a match prints from group ``first``: a credit balance
    (``-$`` or ``CR``) is negative."""
    value = amount(match.group(first + 1))
    return -value if match.group(first) or match.group(first + 2) else value


def _period(text: str) -> tuple[dt.date, dt.date]:
    found = [match.groups() for match in _PERIOD.finditer(text)]
    if not found:
        raise NotRecognised("not a Vancity Visa statement")
    first_month, first_day, first_year, last_month, last_day, year = one_value(found)
    end = written_date(f"{last_month} {last_day} {year}", "%B %d %Y")
    start_year = first_year or year
    start = written_date(f"{first_month} {first_day} {start_year}", "%B %d %Y")
    if first_year is None and start > end:
        start = start.replace(year=start.year - 1)
    if start > end:
        raise NotRecognised("not a Vancity Visa statement")
    return start, end


def _category(text: str, label: str) -> Decimal:
    pattern = re.compile(
        rf"^[ \t]*{re.escape(label)}[ \t]+[-+]?[ \t]*\$({AMOUNT_TEXT})\b",
        re.MULTILINE,
    )
    return amount(one_value([match.group(1) for match in pattern.finditer(text)]))


def _statement(text: str) -> _Statement:
    start, end = _period(text)
    four = one_value([match.group(1) for match in _CARD.finditer(text)])
    previous = re.compile(rf"^[ \t]*Previous balance{_BALANCE}", re.MULTILINE)
    balance = re.compile(rf"Statement balance{_BALANCE}")
    minimum = re.compile(rf"Minimum payment{_BALANCE}")
    due = re.compile(r"Payment due date[ \t]+([A-Z][a-z]+ \d{1,2}, \d{4})")
    minimums = [_signed_balance(match, 1) for match in minimum.finditer(text)]
    return _Statement(
        start=start,
        end=end,
        four=four,
        previous=one_value([_signed_balance(m, 1) for m in previous.finditer(text)]),
        balance=one_value([_signed_balance(m, 1) for m in balance.finditer(text)]),
        debits=sum((_category(text, label) for label in _DEBITS), Decimal(0)),
        credits=sum((_category(text, label) for label in _CREDITS), Decimal(0)),
        minimum=one_value(minimums) if minimums else None,
        due=written_date(
            one_value([match.group(1) for match in due.finditer(text)]), "%B %d, %Y"
        ),
    )


def _lines(text: str, registered: RegisteredAccount, end: dt.date) -> list[ParsedLine]:
    lines: list[ParsedLine] = []
    for row in text.splitlines():
        foreign = _FOREIGN.match(row)
        if foreign is not None:
            if not lines or lines[-1].original_currency is not None:
                raise NotRecognised("a foreign amount with no purchase before it")
            last = lines[-1]
            original = Decimal(foreign.group(1).replace(",", ""))
            lines[-1] = dataclasses.replace(
                last,
                original_amount=original if (last.amount or 0) > 0 else -original,
                original_currency=foreign.group(2),
                stated_cad=last.amount,
            )
            continue
        match = _ROW.match(row)
        if match is None:
            continue
        txn_month, txn_day, post_month, post_day, description, credit, printed = (
            match.groups()
        )
        value = amount(printed)
        lines.append(
            ParsedLine(
                "transaction",
                account=registered,
                posting_date=day_in_period(
                    MONTH_ABBREVIATIONS[post_month], int(post_day), end
                ),
                txn_date=day_in_period(
                    MONTH_ABBREVIATIONS[txn_month], int(txn_day), end
                ),
                amount=value if credit else -value,
                currency=CURRENCY,
                description_raw=" ".join(description.split()),
            )
        )
    return lines


def _is_card_payment(line: ParsedLine) -> bool:
    if line.description_raw is None:
        return False
    hints = classify(
        normalize_description(line.description_raw),
        account_kind=ACCOUNT_KIND,
        issuer=ISSUER,
    )
    return hints.transfer_hint == CARD_PAYMENT


def parse(text: str, accounts: AccountLookup) -> ParseResult:
    statement = _statement(text)
    period = (statement.start, statement.end)
    registered = card_account(accounts, INSTITUTION, statement.four, period)
    lines = [
        with_hints(line, account_kind=ACCOUNT_KIND, issuer=ISSUER)
        for line in _lines(text, registered, statement.end)
    ]
    payments = sum(
        ((line.amount or Decimal(0)) for line in lines if _is_card_payment(line)),
        Decimal(0),
    )
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
                total_debits=statement.debits,
                total_credits=statement.credits,
                total_payments=payments,
                balance_due=statement.balance,
                minimum_due=statement.minimum,
                due_date=statement.due,
            ),
        ),
        lines=tuple(lines),
        control_kind="summary",
    )


def filing_date(text: str) -> dt.date:
    """The statement's period end, its statement date."""
    return _period(text)[1]


PARSER: Final = register_parser(
    Parser(
        parser_id="vancity_visa.statement",
        issuer=ISSUER,
        format="pdf",
        version="1",
        parse=parse,
        filing_date=filing_date,
        sets_hints=True,
    )
)
