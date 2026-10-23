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

A payment's description prints the payment's own number (``Payment #<number>``). The
line stores ``payment_ref_digest`` (restricted), the first 16 hex digits of a sha256
over it, never the number: stage 3 keys a receipt line by it, so two genuine receipts
that print the same date, amount and type stay two, while a re-saved copy of one
receipt gives the same keys. A payment whose description prints no number has none.

The check: the payments sum to ``Total Payments``, else the receipt is rejected
(``checks_failed``) with zero lines.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import re
from dataclasses import dataclass
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
# A payment's own number in its description: ``Payment #<number>``.
_REFERENCE: Final = re.compile(r"#\s*(?P<ref>[A-Za-z0-9]+)")


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


@dataclass(frozen=True)
class _Payment:
    day: dt.date
    kind: str
    value: Decimal
    ref_digest: str | None


def payment_ref_digest(reference: str) -> str:
    """The stored stand-in for a payment's own number: 16 hex digits of a sha256."""
    data = f"receipt-payment:{reference}".encode()
    return hashlib.sha256(data).hexdigest()[:16]


def _payment(line: str) -> _Payment:
    cells = re.split(r"\s{2,}", line.strip())
    money = _MONEY.fullmatch(cells[-1])
    if len(cells) < 3 or _DAY.fullmatch(cells[0]) is None or money is None:
        raise NotRecognised("a line this receipt layout does not print")
    value = amount(money["amount"])
    reference = _REFERENCE.search(" ".join(cells[2:-1]))
    return _Payment(
        day=written_date(cells[0], "%d-%b-%y"),
        kind=cells[1],
        value=-value if money["minus"] else value,
        ref_digest=payment_ref_digest(reference["ref"]) if reference else None,
    )


def _payments(lines: list[str]) -> tuple[list[_Payment], Decimal]:
    headers = [index for index, line in enumerate(lines) if _squeezed(line) == _HEADER]
    if len(headers) != 1:
        raise NotRecognised("a patient receipt without one payment table")
    payments: list[_Payment] = []
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
    if sum((each.value for each in payments), Decimal(0)) != total:
        raise Rejected("checks_failed")
    return ParseResult(
        periods=(),
        lines=tuple(
            ParsedLine(
                "receipt",
                account=None,
                posting_date=each.day,
                amount=-each.value,
                currency=CURRENCY,
                description_raw=f"Patient receipt: {each.kind}",
                counterparty=payee,
                payment_ref_digest=each.ref_digest,
            )
            for each in payments
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
        # 2: each line carries its payment's reference digest.
        version="2",
        parse=parse,
        filing_date=filing_date,
    )
)
