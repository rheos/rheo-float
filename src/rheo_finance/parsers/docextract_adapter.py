"""The one door to ``docextract`` (FR-7, Key decision 1).

The Vancity and Virgin parsing rules live in ``docextract``; this repo imports them and
keeps no copy. This is the only module that imports ``docextract``, and it imports only
``vancity.parse``, ``VancityParseError``, ``reference.parse``, ``pdftext.pdf_to_text``,
``NotTextPDF`` and ``schema.to_money``: nothing from ``monitor/`` or
``docextract.extractor``, because sending a bill that has a working parser to a model
is what FR-47 forbids.

- ``pdf_text(path)`` is the PDF text every fixed-rule parser reads (pdftotext
  ``-layout`` through docextract, so it is in the outbound allowlist), so a parser
  takes extracted text, never a path. A file with no text layer raises
  ``NoTextLayer``.
- ``vancity_statement(text)`` is ``docextract.vancity.parse`` with its bare dict turned
  into typed values, every amount through ``to_money``. The ``checks`` list becomes a
  count: its messages carry account names and amounts, and go no further.
- ``reference_bill(text)`` and ``money(value)`` pass ``docextract.reference.parse`` and
  ``to_money`` through for the Virgin bill parser; text docextract cannot read as a
  bill raises ``NotRecognised``.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

from docextract.pdftext import NotTextPDF, pdf_to_text
from docextract.reference import parse as _reference_parse
from docextract.schema import to_money
from docextract.vancity import VancityParseError
from docextract.vancity import parse as _vancity_parse

from rheo_finance.parsers.base import NoTextLayer, NotRecognised


def pdf_text(path: Path) -> str:
    """The layout text of the PDF at ``path``."""
    try:
        return pdf_to_text(path)
    except NotTextPDF:
        raise NoTextLayer("the PDF has no usable text layer") from None


def money(value: object) -> Decimal | None:
    """``docextract.schema.to_money``: an exact two-decimal ``Decimal``, or None."""
    return to_money(value)


def reference_bill(text: str) -> Mapping[str, Any]:
    """``docextract.reference.parse`` over a Virgin Plus bill's text; ``NotRecognised``
    when it cannot read the text as a bill (it fails on a missing bill or due date
    rather than returning nothing)."""
    try:
        return _reference_parse(text)
    except (AttributeError, ValueError):
        raise NotRecognised("not a Virgin Plus bill layout") from None


@dataclass(frozen=True)
class VancityTransaction:
    date: dt.date
    description: str
    withdrawal: Decimal | None
    deposit: Decimal | None
    # None where the statement prints no balance and docextract could not settle one.
    balance: Decimal | None
    fee_type: str | None


@dataclass(frozen=True)
class VancitySpell:
    start: dt.date
    end: dt.date | None


@dataclass(frozen=True)
class VancityAccount:
    """One account section. ``ref`` is the account number's last four digits; the
    printed name is dropped (an account can be renamed and keep its number)."""

    ref: str
    opening: Decimal
    withdrawals: Decimal
    deposits: Decimal
    closing: Decimal
    overdrawn: tuple[VancitySpell, ...]
    transactions: tuple[VancityTransaction, ...]


@dataclass(frozen=True)
class VancityStatement:
    period_start: dt.date
    period_end: dt.date
    accounts: tuple[VancityAccount, ...]
    # How many of docextract's checks failed; any at all rejects the document.
    failed_checks: int


def _amount(value: object) -> Decimal:
    converted = to_money(value)
    if converted is None:
        raise ValueError("docextract returned no amount where one is required")
    return converted


def _date(value: object) -> dt.date:
    return dt.date.fromisoformat(str(value))


def vancity_statement(text: str) -> VancityStatement:
    """Parse a Vancity statement's layout text; ``NotRecognised`` when docextract does
    not read it as one."""
    try:
        parsed = _vancity_parse(text)
    except VancityParseError:
        raise NotRecognised("not a Vancity statement layout") from None
    accounts = tuple(
        VancityAccount(
            ref=str(account["ref"]),
            opening=_amount(account["opening"]),
            withdrawals=_amount(account["withdrawals"]),
            deposits=_amount(account["deposits"]),
            closing=_amount(account["closing"]),
            overdrawn=tuple(
                VancitySpell(
                    start=_date(spell["from"]),
                    end=None if spell["to"] is None else _date(spell["to"]),
                )
                for spell in account["overdrawn"]
            ),
            transactions=tuple(
                VancityTransaction(
                    date=_date(txn["date"]),
                    description=str(txn["description"]),
                    withdrawal=to_money(txn["withdrawal"]),
                    deposit=to_money(txn["deposit"]),
                    balance=to_money(txn["balance"]),
                    fee_type=None if txn["fee_type"] is None else str(txn["fee_type"]),
                )
                for txn in account["transactions"]
            ),
        )
        for account in parsed["accounts"]
    )
    return VancityStatement(
        period_start=_date(parsed["period_start"]),
        period_end=_date(parsed["period_end"]),
        accounts=accounts,
        failed_checks=len(parsed["checks"]),
    )
