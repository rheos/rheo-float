"""An invoice received (PDF), the invoice series filed under ``invoices-received/``
(FR-8, FR-10, FR-16).

Registered for the ``invoices-received`` directory, so it reads the files of every
sub-directory there (``parsers.registry.parsers_for``). The layout, as the series on
file prints it:

- ``Invoice <number>`` on the first line, then ``Issue date: <weekday>, <Mon> <d>,
  <yyyy>`` and ``Due date:`` in the same form;
- a ``FROM`` block whose first name line is the issuer (the payee), and a ``TO``
  block (the owner, read past);
- an item table under ``Description   Qty   Unit price   Amount``: every line until
  ``Subtotal`` is one item, ``<description>   <qty>   $<unit price>   $<amount>``;
- ``Subtotal``, ``Total``, ``Paid`` and ``Amount due``, each with its amount. Text
  after ``Amount due`` (the generator's footer) is read past.

What it gives: one ``invoice`` line and no ``statement_period``, control ``none``. The
line has no account (an invoice is evidence of a payment, matched to a payment line
in stage 3, never a txn by itself), ``posting_date`` the issue date, ``amount`` the
invoice total signed as the owner's outflow (negative), ``currency`` CAD (the series
prints dollars and no other currency), ``description_raw`` ``Invoice <number>`` and
``counterparty`` (restricted) the payee normalized as descriptions are
(``text.normalize_description``). Nothing else from the invoice is stored: not the
item descriptions, the owner's block or the due date.

The amount is the total, not the amount still due, so a payment of the whole invoice
matches it in stage 3 even once the invoice prints itself paid in full (amount due
zero). What is still due on a part-paid invoice is an open-item question for the
stages that track open items, not this line's amount.

The identity: the items sum to the subtotal, the subtotal is the total (the layout
prints no tax line; a line it does not know between them makes the invoice
``NotRecognised``), and the total less the paid amount is the amount due. An invoice
that breaks it is rejected (``identity_mismatch``) with zero lines.
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

ISSUER: Final = "invoices-received"
CURRENCY: Final = "CAD"
_INVOICE: Final = re.compile(r"\s*Invoice\s+(?P<number>[A-Za-z0-9][A-Za-z0-9-]*)\s*")
_DATE: Final = re.compile(r"\s*(?P<label>Issue|Due) date:\s+(?P<date>\S.*?)\s*")
_HEADER: Final = "DescriptionQtyUnitpriceAmount"
_ITEM: Final = re.compile(
    rf"\s*(?P<description>\S.*?)\s{{2,}}(?P<qty>\d+(?:\.\d+)?)"
    rf"\s{{2,}}\$(?P<price>{AMOUNT_TEXT})\s{{2,}}\$(?P<amount>{AMOUNT_TEXT})\s*"
)
_SUMMARY: Final = re.compile(
    rf"\s*(?P<label>[A-Za-z ]+?)\s{{2,}}\$(?P<amount>{AMOUNT_TEXT})\s*"
)
SUMMARY_LABELS: Final = ("Subtotal", "Total", "Paid", "Amount due")


def _squeezed(line: str) -> str:
    return "".join(line.split())


def _date(text: str) -> dt.date:
    """``Mon, Dec 1, 2025``: the weekday is read past."""
    weekday, _, rest = text.partition(", ")
    if not weekday.isalpha() or len(weekday) != 3:
        raise NotRecognised("not an invoice date")
    return written_date(rest, "%b %d, %Y")


def _dates(lines: list[str]) -> dict[str, dt.date]:
    found: dict[str, dt.date] = {}
    for line in lines:
        match = _DATE.fullmatch(line)
        if match is not None:
            if match["label"] in found:
                raise NotRecognised("an invoice date printed twice")
            found[match["label"]] = _date(match["date"])
    if set(found) != {"Issue", "Due"}:
        raise NotRecognised("an invoice without its issue and due dates")
    return found


def _payee(lines: list[str]) -> str:
    """The first name line of the ``FROM`` block (the due date line, which the layout
    prints inside it, is read past)."""
    starts = [index for index, line in enumerate(lines) if line.strip() == "FROM"]
    if len(starts) != 1:
        raise NotRecognised("an invoice without one FROM block")
    for line in lines[starts[0] + 1 :]:
        if line.strip() == "TO":
            break
        if line.strip() and _DATE.fullmatch(line) is None:
            return " ".join(line.split())
    raise NotRecognised("an invoice without its issuer")


def _items_and_summary(lines: list[str]) -> tuple[list[Decimal], dict[str, Decimal]]:
    headers = [index for index, line in enumerate(lines) if _squeezed(line) == _HEADER]
    if len(headers) != 1:
        raise NotRecognised("an invoice without one item table")
    items: list[Decimal] = []
    summary: dict[str, Decimal] = {}
    for line in lines[headers[0] + 1 :]:
        if not line.strip():
            continue
        found = _SUMMARY.fullmatch(line)
        if found is not None and found["label"] in SUMMARY_LABELS:
            expected = SUMMARY_LABELS[len(summary)]
            if found["label"] != expected:
                raise NotRecognised("invoice totals out of their order")
            summary[expected] = amount(found["amount"])
            if expected == SUMMARY_LABELS[-1]:
                break
            continue
        item = _ITEM.fullmatch(line)
        if item is None or summary:
            raise NotRecognised("a line this invoice layout does not print")
        items.append(amount(item["amount"]))
    if not items or tuple(summary) != SUMMARY_LABELS:
        raise NotRecognised("an invoice without its items and totals")
    return items, summary


def _read(text: str) -> tuple[str, dt.date, list[Decimal], dict[str, Decimal], str]:
    lines = text.splitlines()
    filled = [line for line in lines if line.strip()]
    first = _INVOICE.fullmatch(filled[0]) if filled else None
    if first is None:
        raise NotRecognised("not an invoice")
    issued = _dates(lines)["Issue"]
    items, summary = _items_and_summary(lines)
    return first["number"], issued, items, summary, _payee(lines)


def parse(text: str, accounts: AccountLookup) -> ParseResult:
    number, issued, items, summary, payee = _read(text)
    holds = (
        sum(items, Decimal(0)) == summary["Subtotal"]
        and summary["Subtotal"] == summary["Total"]
        and summary["Total"] - summary["Paid"] == summary["Amount due"]
    )
    if not holds:
        raise Rejected("identity_mismatch")
    line = ParsedLine(
        "invoice",
        account=None,
        posting_date=issued,
        amount=-summary["Total"],
        currency=CURRENCY,
        description_raw=f"Invoice {number}",
        counterparty=normalize_description(payee),
    )
    return ParseResult(periods=(), lines=(line,), control_kind="none")


def filing_date(text: str) -> dt.date:
    """The issue date, the date the filer names a new copy by."""
    return _read(text)[1]


PARSER: Final = register_parser(
    Parser(
        parser_id="invoices_received.invoice",
        issuer=ISSUER,
        format="pdf",
        # 2: the line's amount is the invoice total, no longer the amount due.
        version="2",
        parse=parse,
        filing_date=filing_date,
    )
)
