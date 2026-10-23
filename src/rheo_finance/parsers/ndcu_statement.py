"""The Nelson & District Credit Union monthly statement (PDF), two layout versions
(FR-3 ``summary``, FR-8, FR-15(a)).

One template, two renderings. The statements from August 2025 on (version 1) come
out of pdftotext with their labels letter-spaced ("St at ement Of Account s",
"Withdraw als"); the older ones, 2018 among them (version 2), print the same labels
whole ("Statement Of Accounts"). Each version reads only its own rendering, so the
two register separately and the document records which one read it. Labels are
compared with their spaces removed.

The layout: a header with the statement date (the period end), then one section
per account under a heading (``CHEQUING``, ``SAVINGS``, ``SHARES``; a section that
runs onto the next page repeats its heading with "(continued)" and its account
line). Each section prints ``Account # <number>``, an ``Opening Balance`` line dated
the period start, transaction lines ``DD Mon YY  <description>  <amount>  <balance>``
whose description may wrap onto indented lines, and closes with ``Total Withdrawals
and Deposits $<withdrawals> $<deposits>``.

Wrapped descriptions. The text comes through ``docextract`` compacted (every run of
four or more spaces becomes three), so a wrapped line, a page footer and a header
all start with the same three spaces and the description column cannot be read from
the indentation. A wrap is told by its place and shape instead: indented lines right
after a transaction line, made only of name characters (letters, spaces, ``&``,
apostrophes and hyphens: no digit and no dot, so neither a page number nor a web
address is name-shaped), and followed by the next dated line or the page's
"- continued -" marker. Any other indented text right after a transaction line (a
footer, a URL, a number) is a layout this parser has not seen, and the statement is
``NotRecognised``: it never joins a description. A footer made only of name
characters is stopped by its place alone: it joins nothing unless the next dated
line or the marker follows it.

Headings. Every account line needs a heading of its own above it (a continued
section repeats its heading), and a heading-shaped line (capitals at the start of a
line, spaces allowed) that is not one of ``HEADINGS`` is ``NotRecognised``, so an
account never takes the kind of the section before it, and a section in another
currency is never read as CAD.

- Each account gives one CAD ``statement_period``, control ``summary``: opening, the
  last balance as closing, and the printed totals. The registry ties it out.
- A line prints one unsigned amount; its side is read from the running balance (the
  balance before it less the amount, a withdrawal, or plus it, a deposit). A line the
  balance does not continue either way rejects the statement (``checks_failed``); the
  printed totals then check every side the balance chain chose.
- An account is matched by its full number among the registry's ``number``
  identifiers at ``INSTITUTION``; the number is stored nowhere. An unregistered
  number rejects the statement (``unknown_account``), two accounts holding it
  ``ambiguous_account``.
- Every line carries the hints its wording gives (``parsers.hints.with_hints``): an
  e-Transfer's ``Payee:`` or ``Payer:`` name becomes its restricted ``counterparty``,
  and "EMT Transfer - Debit Reversal Customer Cancel" its ``reversal_hint``.

Text that is not this layout (no title, no statement date, a section heading or a
dated line it cannot read, a section without its opening or totals) is
``NotRecognised``.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Final, Literal

from rheo_finance.parsers.base import (
    AccountLookup,
    NotRecognised,
    ParsedLine,
    ParsedPeriod,
    ParseResult,
    RegisteredAccount,
    Rejected,
)
from rheo_finance.parsers.card import AMOUNT_TEXT, amount, written_date
from rheo_finance.parsers.hints import with_hints
from rheo_finance.parsers.registry import Parser, register_parser

INSTITUTION: Final = "Nelson & District Credit Union"
ISSUER: Final = "nelson-district-cu"
CURRENCY: Final = "CAD"
Version = Literal["1", "2"]
# The section headings this layout prints, with the account kind the hints read.
# shortcut: the membership share account is classified as savings, since no pattern
# tells an equity share from a savings account; split them if one ever does.
HEADINGS: Final = {"CHEQUING": "chequing", "SAVINGS": "savings", "SHARES": "savings"}

_DAY: Final = r"\d\d [A-Z][a-z]{2} \d\d"
_LINE: Final = re.compile(
    rf"(?P<day>{_DAY})\s+(?P<description>\S.*?)\s{{2,}}(?P<first>{AMOUNT_TEXT})"
    rf"(?:\s{{2,}}(?P<second>{AMOUNT_TEXT}))?\s*"
)
_DATED: Final = re.compile(rf"{_DAY}\s")
_STATEMENT_DATE: Final = re.compile(
    r"\s*(?P<label>.+?)\s{2,}(?P<date>[A-Z][a-z]+ \d{1,2}, \d{4})\s*"
)
_HEADING: Final = re.compile(r"(?P<heading>[A-Z][A-Z&\- ]*?)\s*(?:\(.*\))?\s*")
_NAME: Final = re.compile(r"[A-Za-z][A-Za-z&'\- ]*")
_ACCOUNT: Final = re.compile(r"Account #\s*(?P<number>\d+)\b.*")
_TOTAL: Final = re.compile(rf"\$({AMOUNT_TEXT})")


def _squeezed(line: str) -> str:
    return "".join(line.split())


@dataclass
class _Row:
    day: dt.date
    description: str
    amount: Decimal
    balance: Decimal


@dataclass
class _Section:
    number: str
    kind: str
    opening: Decimal | None = None
    opening_day: dt.date | None = None
    rows: list[_Row] = field(default_factory=list)
    totals: tuple[Decimal, Decimal] | None = None


@dataclass(frozen=True)
class _Statement:
    end: dt.date
    sections: tuple[_Section, ...]


def _version(lines: list[str]) -> Version:
    titles = [line for line in lines if _squeezed(line) == "StatementOfAccounts"]
    if not titles:
        raise NotRecognised("not a Nelson & District Credit Union statement")
    return "2" if titles[0].strip() == "Statement Of Accounts" else "1"


def _statement_end(lines: list[str]) -> dt.date:
    found = set()
    for line in lines:
        match = _STATEMENT_DATE.fullmatch(line)
        if match is not None and _squeezed(match["label"]) == "Date":
            found.add(written_date(match["date"], "%B %d, %Y"))
    if len(found) != 1:
        raise NotRecognised("no single statement date")
    return found.pop()


def _marker(line: str) -> bool:
    return _squeezed(line).casefold() == "-continued-"


def _wrapped(lines: list[str], at: int) -> list[str]:
    """The wrapped lines of the description on line ``at - 1``: the indented lines
    from ``at`` on, when they are name-shaped and the next dated line or the
    continued marker follows them."""
    end = at
    while end < len(lines) and lines[end][:1].isspace() and lines[end].strip():
        if _marker(lines[end]):
            break
        end += 1
    run = lines[at:end]
    if not run:
        return []
    after = lines[end] if end < len(lines) else ""
    named = all(_NAME.fullmatch(line.strip()) for line in run)
    if not named or not (_DATED.match(after) or _marker(after)):
        raise NotRecognised("text after a transaction line this layout does not print")
    return [line.strip() for line in run]


def _read(text: str, version: Version) -> _Statement:
    lines = text.splitlines()
    if _version(lines) != version:
        raise NotRecognised("the other Nelson & District Credit Union layout")
    end = _statement_end(lines)
    sections: dict[str, _Section] = {}
    kind: str | None = None
    current: _Section | None = None
    for index, line in enumerate(lines):
        heading = _HEADING.fullmatch(line)
        if heading is not None:
            if heading["heading"] not in HEADINGS:
                raise NotRecognised("a section heading this layout does not print")
            kind, current = HEADINGS[heading["heading"]], None
            continue
        account = _ACCOUNT.fullmatch(line)
        if account is not None:
            if kind is None:
                raise NotRecognised("an account line without its own heading")
            current = sections.setdefault(
                account["number"], _Section(account["number"], kind)
            )
            if current.kind != kind:
                raise NotRecognised("one account under two headings")
            kind = None  # the next account line needs its own heading
            continue
        if _squeezed(line).startswith("TotalWithdrawalsandDeposits"):
            totals = _TOTAL.findall(line)
            if current is None or current.totals is not None or len(totals) != 2:
                raise NotRecognised("a totals line this layout does not print")
            current.totals = (amount(totals[0]), amount(totals[1]))
            current = None
            continue
        dated = _LINE.fullmatch(line.rstrip())
        if dated is not None:
            row = _dated(current, dated)
            if row is not None:
                row.description = " ".join(
                    [row.description, *_wrapped(lines, index + 1)]
                )
            continue
        if _DATED.match(line):
            raise NotRecognised("a dated line this layout does not print")
    return _Statement(end, tuple(sections.values()))


def _dated(current: _Section | None, match: re.Match[str]) -> _Row | None:
    """Store one dated line in its section: the opening balance, or a row."""
    if current is None:
        raise NotRecognised("a dated line outside an account section")
    day = written_date(match["day"], "%d %b %y")
    first = amount(match["first"])
    if _squeezed(match["description"]) == "OpeningBalance":
        if match["second"] is not None or current.opening is not None:
            raise NotRecognised("an opening balance this layout does not print")
        current.opening, current.opening_day = first, day
        return None
    if match["second"] is None or current.opening is None:
        raise NotRecognised("a line without its balance or opening")
    row = _Row(
        day, " ".join(match["description"].split()), first, amount(match["second"])
    )
    current.rows.append(row)
    return row


def _match(
    statement: _Statement, period: tuple[dt.date, dt.date], accounts: AccountLookup
) -> list[tuple[_Section, RegisteredAccount]]:
    found = [
        (section, accounts.by_identifier(INSTITUTION, section.number, *period))
        for section in statement.sections
    ]
    named = tuple(dict.fromkeys(each[0] for _, each in found if len(each) == 1))
    for _, each in found:
        if not each:
            raise Rejected("unknown_account", period, named)
        if len(each) > 1:
            raise Rejected("ambiguous_account", period, named)
    if len(named) != len(found):
        raise Rejected("ambiguous_account", period, named)
    return [(section, each[0]) for section, each in found]


def _signed(section: _Section) -> list[Decimal] | None:
    """Each row's signed amount, read from the running balance; None when a row's
    balance does not follow from the one before it."""
    previous = section.opening
    signed: list[Decimal] = []
    for row in section.rows:
        if previous is None:
            return None
        if previous - row.amount == row.balance:
            signed.append(-row.amount)
        elif previous + row.amount == row.balance:
            signed.append(row.amount)
        else:
            return None
        previous = row.balance
    return signed


def _period_start(statement: _Statement) -> dt.date:
    """The one opening day every section prints; each section must print its opening
    balance and its totals."""
    days = {section.opening_day for section in statement.sections}
    if not statement.sections or len(days) != 1:
        raise NotRecognised("sections without one opening day")
    (day,) = days
    if day is None or any(each.totals is None for each in statement.sections):
        raise NotRecognised("a section without its opening balance or totals")
    return day


def _parse(text: str, accounts: AccountLookup, version: Version) -> ParseResult:
    statement = _read(text, version)
    start = _period_start(statement)
    period = (start, statement.end)
    matched = _match(statement, period, accounts)
    periods: list[ParsedPeriod] = []
    lines: list[ParsedLine] = []
    for section, registered in matched:
        signed = _signed(section)
        opening, totals = section.opening, section.totals
        if signed is None or opening is None or totals is None:
            raise Rejected("checks_failed", period, tuple(r for _, r in matched))
        closing = section.rows[-1].balance if section.rows else opening
        periods.append(
            ParsedPeriod(
                account=registered,
                currency=CURRENCY,
                period_start=start,
                period_end=statement.end,
                control_kind="summary",
                opening=opening,
                closing=closing,
                total_debits=totals[0],
                total_credits=totals[1],
            )
        )
        for row, value in zip(section.rows, signed, strict=True):
            line = ParsedLine(
                "transaction",
                account=registered,
                posting_date=row.day,
                amount=value,
                currency=CURRENCY,
                description_raw=row.description,
                running_balance=row.balance,
            )
            lines.append(with_hints(line, account_kind=section.kind, issuer=ISSUER))
    return ParseResult(
        periods=tuple(periods), lines=tuple(lines), control_kind="summary"
    )


def parse_current(text: str, accounts: AccountLookup) -> ParseResult:
    """The layout from August 2025 on (version 1)."""
    return _parse(text, accounts, "1")


def parse_2018(text: str, accounts: AccountLookup) -> ParseResult:
    """The older layout, 2018 among it (version 2)."""
    return _parse(text, accounts, "2")


def filing_date_current(text: str) -> dt.date:
    return _read(text, "1").end


def filing_date_2018(text: str) -> dt.date:
    return _read(text, "2").end


CURRENT: Final = register_parser(
    Parser(
        parser_id="nelson_district_cu.statement",
        issuer=ISSUER,
        format="pdf",
        version="1",
        parse=parse_current,
        filing_date=filing_date_current,
        sets_hints=True,
    )
)
LAYOUT_2018: Final = register_parser(
    Parser(
        parser_id="nelson_district_cu.statement_2018",
        issuer=ISSUER,
        format="pdf",
        version="2",
        parse=parse_2018,
        filing_date=filing_date_2018,
        sets_hints=True,
    )
)
