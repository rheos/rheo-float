"""A patient receipt (PDF) from a clinic, filed under ``receipts/`` (FR-8, FR-10).

Registered for the ``receipts`` directory, so it reads the files of every
sub-directory there (``parsers.registry.parsers_for``); any other receipt layout stays
``unparsed``. The layout, as the receipt on file prints it:

- the first line is ``<clinic>   Patient Receipt``: the clinic is the payee;
- ``Print date:   <dd>-<Mon>-<yyyy>`` among the clinic's address lines;
- a payment table under ``Date   Payment Type   Description   Payment Amount``: every
  line until ``Total Payments`` is one payment, ``<dd>-<Mon>-<yy>   <type>
  [<description>]   $<amount>``;
- ``Total Payments   $<amount>``. What follows (the balance, the treatment table) is
  read past and stored nowhere.

What it gives: one ``receipt`` line per payment and no ``statement_period``, control
``none``. A line has no account (a receipt is evidence of a payment, matched to a
payment line in stage 3), ``posting_date`` the payment date, ``amount`` the payment
signed as the owner's outflow (negative), ``currency`` CAD (the receipt prints
dollars and no other currency), ``description_raw`` ``Patient receipt: <payment
type>`` and ``counterparty`` (restricted) the clinic normalized as descriptions are
(``text.normalize_description``). The patient's name and address, the payment
descriptions and the treatment lines are stored nowhere.

The check: the payments sum to ``Total Payments``, else the receipt is rejected
(``checks_failed``) with zero lines.
"""

from __future__ import annotations

import datetime as dt
import re
from decimal import Decimal
from typing import Final

from rheo_finance.parsers.base import (
    AccountLookup,
    NotRecognised,
    ParsedLine,
    ParseResult,
    Rejected,
)
from rheo_finance.parsers.card import AMOUNT_TEXT, amount, written_date
from rheo_finance.parsers.registry import Parser, register_parser
from rheo_finance.text import normalize_description

ISSUER: Final = "receipts"
CURRENCY: Final = "CAD"
_TITLE: Final = re.compile(r"\s*(?P<payee>\S.*?)\s{2,}Patient Receipt\s*")
_PRINT_DATE: Final = re.compile(
    r".*\s{2,}Print date:\s+(?P<date>\d\d-[A-Z][a-z]{2}-\d{4})\s*"
)
_HEADER: Final = "DatePaymentTypeDescriptionPaymentAmount"
_DAY: Final = re.compile(r"\d\d-[A-Z][a-z]{2}-\d\d")
_MONEY: Final = re.compile(rf"(?P<minus>-)?\$(?P<amount>{AMOUNT_TEXT})")
_TOTAL: Final = re.compile(rf"\s*Total Payments\s{{2,}}\$(?P<amount>{AMOUNT_TEXT})\s*")


def _squeezed(line: str) -> str:
    return "".join(line.split())


def _first_line(lines: list[str]) -> str:
    filled = [line for line in lines if line.strip()]
    title = _TITLE.fullmatch(filled[0]) if filled else None
    if title is None:
        raise NotRecognised("not a patient receipt")
    return " ".join(title["payee"].split())


def _print_date(lines: list[str]) -> dt.date:
    found = {match["date"] for line in lines if (match := _PRINT_DATE.fullmatch(line))}
    if len(found) != 1:
        raise NotRecognised("a patient receipt without one print date")
    return written_date(found.pop(), "%d-%b-%Y")


def _payment(line: str) -> tuple[dt.date, str, Decimal]:
    cells = re.split(r"\s{2,}", line.strip())
    money = _MONEY.fullmatch(cells[-1])
    if len(cells) < 3 or _DAY.fullmatch(cells[0]) is None or money is None:
        raise NotRecognised("a line this receipt layout does not print")
    value = amount(money["amount"])
    return (
        written_date(cells[0], "%d-%b-%y"),
        cells[1],
        -value if money["minus"] else value,
    )


def _payments(lines: list[str]) -> tuple[list[tuple[dt.date, str, Decimal]], Decimal]:
    headers = [index for index, line in enumerate(lines) if _squeezed(line) == _HEADER]
    if len(headers) != 1:
        raise NotRecognised("a patient receipt without one payment table")
    payments: list[tuple[dt.date, str, Decimal]] = []
    for line in lines[headers[0] + 1 :]:
        if not line.strip():
            continue
        total = _TOTAL.fullmatch(line)
        if total is not None:
            if not payments:
                raise NotRecognised("a patient receipt without a payment")
            return payments, amount(total["amount"])
        payments.append(_payment(line))
    raise NotRecognised("a patient receipt without its total payments")


def parse(text: str, accounts: AccountLookup) -> ParseResult:
    lines = text.splitlines()
    payee = normalize_description(_first_line(lines))
    _print_date(lines)
    payments, total = _payments(lines)
    if sum((value for _, _, value in payments), Decimal(0)) != total:
        raise Rejected("checks_failed")
    return ParseResult(
        periods=(),
        lines=tuple(
            ParsedLine(
                "receipt",
                account=None,
                posting_date=day,
                amount=-value,
                currency=CURRENCY,
                description_raw=f"Patient receipt: {kind}",
                counterparty=payee,
            )
            for day, kind, value in payments
        ),
        control_kind="none",
    )


def filing_date(text: str) -> dt.date:
    """The print date, the date the filer names a new copy by."""
    lines = text.splitlines()
    _first_line(lines)
    return _print_date(lines)


PARSER: Final = register_parser(
    Parser(
        parser_id="receipts.patient_receipt",
        issuer=ISSUER,
        format="pdf",
        version="1",
        parse=parse,
        filing_date=filing_date,
    )
)
