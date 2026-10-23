"""The Vancity Visa transaction download (PDF layout text), control ``none`` (FR-3,
FR-8).

The layout (what ``pdf_text`` prints):

- ``Transactions for <card product> card``, then the range ``<Mon> <day>, <year> to
  <Mon> <day>, <year>``, the account box (limit, balance, last payment), the
  cardholder's name beside the masked card (``999999XXXXXX9999``), and the table
  header ``Transaction Date   Posted Date   Description   Currency   Debit   Credit``.
- One row per transaction: ``<transaction date>   <posted date>   <description>
  CAD   $<amount>``, a credit printed ``-$<amount>``. A long description runs on to
  the next line(s) until a blank line; a page footer (``<n> of <m> ...``) is not part
  of it.

The name is read past and stored nowhere. The card is matched by its last four at
``INSTITUTION`` (``parsers.card.card_account``). One CAD ``statement_period`` covers
the printed range (widened to any row outside it), control kind ``none``: the download
has no totals and no running balance. A debit is negative, a credit positive.

This parser and ``vancity_visa_statement`` both read the issuer's PDFs; the registry
holds one parser per (issuer, format, version), so this one's version names its layout.

An older download prints the card as ``9999 99•• •••• 9999`` and every amount without a
sign: its Debit and Credit columns are told apart only by position. Plain
``pdftotext -layout`` output does keep them apart (on the real file the two columns
start about a dozen characters apart, a refund among the credits), but ``pdf_text``
returns docextract's compacted text, which shrinks every run of four or more spaces
to three, and after that an amount's offset depends on its description's length: the
two columns overlap. A parser reads only that text, so it cannot tell a refund from a
purchase. Reading the old layout by offset would need a second, uncompacted
extraction through the adapter and the registry, which every PDF parser shares. So
that layout is ``NotRecognised`` and stays ``unparsed``; the export CSV of the same
window holds its rows with their signs. So is any other text that is not this
layout, or a row in a currency other than CAD.
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

ISSUER: Final = "vancity-visa"
INSTITUTION: Final = "Vancity"

_TITLE: Final = re.compile(r"^\s*Transactions for .+ card\s*$", re.MULTILINE)
_SHORT_DATE: Final = r"[A-Z][a-z]{2} \d{1,2}, \d{4}"
_RANGE: Final = re.compile(rf"^\s*({_SHORT_DATE}) to ({_SHORT_DATE})\s*$", re.MULTILINE)
_HEADER: Final = re.compile(
    r"^\s*Transaction Date\s+Posted Date\s+Description\s+Currency\s+Debit\s+Credit\s*$",
    re.MULTILINE,
)
_CARD: Final = re.compile(r"\b\d{6}X{6}(\d{4})\b")
_ISO: Final = r"\d{4}-\d\d-\d\d"
_ROW: Final = re.compile(
    rf"^\s*({_ISO})\s+({_ISO})\s+(.*?)\s+([A-Z]{{3}})\s+(-)?\$({AMOUNT_TEXT})\s*$"
)
_FOOTER: Final = re.compile(r"^\s*\d+ of \d+\b")


@dataclass
class _Row:
    txn_date: dt.date
    posting_date: dt.date
    description: list[str]
    amount: Decimal


def _range(text: str) -> tuple[dt.date, dt.date]:
    if _TITLE.search(text) is None or _HEADER.search(text) is None:
        raise NotRecognised("not a Vancity Visa transaction download")
    first, last = one_value([match.groups() for match in _RANGE.finditer(text)])
    start = written_date(first, "%b %d, %Y")
    end = written_date(last, "%b %d, %Y")
    if start > end:
        raise NotRecognised("not a Vancity Visa transaction download")
    return start, end


def _rows(text: str) -> list[_Row]:
    rows: list[_Row] = []
    open_row = False
    for line in text.splitlines():
        match = _ROW.match(line)
        if match is not None:
            txn_date, posting_date, description, currency, credit, printed = (
                match.groups()
            )
            if currency != CURRENCY:
                raise NotRecognised("a row in a currency this layout does not post")
            value = amount(printed)
            rows.append(
                _Row(
                    txn_date=written_date(txn_date, "%Y-%m-%d"),
                    posting_date=written_date(posting_date, "%Y-%m-%d"),
                    description=[description],
                    amount=value if credit else -value,
                )
            )
            open_row = True
        elif not line.strip() or _FOOTER.match(line):
            open_row = False
        elif open_row:
            rows[-1].description.append(line)
    return rows


def parse(text: str, accounts: AccountLookup) -> ParseResult:
    start, end = _range(text)
    four = one_value([match.group(1) for match in _CARD.finditer(text)])
    rows = _rows(text)
    dates = [row.posting_date for row in rows]
    period = (min([start, *dates]), max([end, *dates]))
    registered = card_account(accounts, INSTITUTION, four, period)
    if dates != sorted(dates):
        # Newest first, as the download lists them: oldest first, same-day rows kept
        # in the order they happened.
        rows.reverse()
        rows.sort(key=lambda row: row.posting_date)
    lines = tuple(
        with_hints(
            ParsedLine(
                "transaction",
                account=registered,
                posting_date=row.posting_date,
                txn_date=row.txn_date,
                amount=row.amount,
                currency=CURRENCY,
                description_raw=" ".join(" ".join(row.description).split()),
            ),
            account_kind=ACCOUNT_KIND,
            issuer=ISSUER,
        )
        for row in rows
    )
    return ParseResult(
        periods=(
            ParsedPeriod(
                account=registered,
                currency=CURRENCY,
                period_start=period[0],
                period_end=period[1],
                control_kind="none",
            ),
        ),
        lines=lines,
        control_kind="none",
    )


def filing_date(text: str) -> dt.date:
    """The end of the printed range, the date the filer names a new copy by."""
    if _CARD.search(text) is None:
        raise NotRecognised("not a Vancity Visa transaction download")
    return _range(text)[1]


PARSER: Final = register_parser(
    Parser(
        parser_id="vancity_visa.transactions",
        issuer=ISSUER,
        format="pdf",
        version="transactions-1",
        parse=parse,
        filing_date=filing_date,
        sets_hints=True,
    )
)
