"""The Virgin Plus mobile bill (PDF), through ``docextract`` (FR-3, FR-7, FR-8).

``docextract.reference.parse`` reads the bill; this module maps its fields onto one CAD
``statement_period`` with control kind ``bill_identity`` and no ledger lines (a bill
feeds obligations, not the ledger):

- every amount goes through ``to_money``, so docextract's floats become exact
  two-decimal values;
- ``balance_due`` is the printed total due and ``due_date`` the "pay by" date;
- the bill prints no service period, so the period is its bill-date month;
- the identity: the printed unpaid balance plus the current charges must equal the
  total due, else the bill is rejected (``identity_mismatch``) with zero lines. A
  bill that prints no unpaid balance or no total due states no identity and loads as
  printed, with ``tied_out`` false; a bill whose identity holds is tied out;
- the bill is matched to the registry by the last four digits of its printed account
  number, at the institution ``INSTITUTION`` (compared by ``institution_key``). An
  account the registry does not hold rejects the bill (``unknown_account``); the
  number itself is never kept.

No model is ever called: a bill with a working fixed-rule parser stays off the model
path (FR-47).
"""

from __future__ import annotations

import calendar
import datetime as dt
import re
from collections.abc import Mapping
from decimal import Decimal
from typing import Any, Final

from rheo_finance.manual.accounts import last_four
from rheo_finance.parsers.base import (
    AccountLookup,
    ParsedPeriod,
    ParseResult,
    RegisteredAccount,
    Rejected,
)
from rheo_finance.parsers.docextract_adapter import money, reference_bill
from rheo_finance.parsers.registry import Parser, register_parser

INSTITUTION: Final = "Virgin Plus"
CURRENCY: Final = "CAD"
# The first "Account Number" line with the number on it (page 1's header block).
_ACCOUNT_NUMBER: Final = re.compile(
    r"^[ \t]*Account Number[ \t]+(\d[\d -]*\d)[ \t]*$", re.M
)


def _date(value: object) -> dt.date:
    return dt.date.fromisoformat(str(value))


def _bill_month(bill_date: dt.date) -> tuple[dt.date, dt.date]:
    last = calendar.monthrange(bill_date.year, bill_date.month)[1]
    return bill_date.replace(day=1), bill_date.replace(day=last)


def _account(
    text: str, accounts: AccountLookup, period: tuple[dt.date, dt.date]
) -> RegisteredAccount:
    match = _ACCOUNT_NUMBER.search(text)
    four = last_four(match.group(1)) if match else None
    if four is None:
        raise Rejected("unknown_account", period)
    found = accounts.by_last_four(INSTITUTION, four, *period)
    if not found:
        raise Rejected("unknown_account", period)
    if len(found) > 1:
        raise Rejected("ambiguous_account", period)
    return found[0]


def _tied_out(bill: Mapping[str, Any], period: tuple[dt.date, dt.date]) -> bool:
    """Whether the bill's identity holds; False when it prints none."""
    unpaid = money(bill["unpaid_balance"])
    total = money(bill["total_due"])
    if unpaid is None or total is None:
        return False
    current = money(bill["current_charges"]) or Decimal("0.00")
    if unpaid + current != total:
        raise Rejected("identity_mismatch", period)
    return True


def parse(text: str, accounts: AccountLookup) -> ParseResult:
    bill = reference_bill(text)
    period = _bill_month(_date(bill["bill_date"]))
    registered = _account(text, accounts, period)
    tied_out = _tied_out(bill, period)
    return ParseResult(
        periods=(
            ParsedPeriod(
                account=registered,
                currency=CURRENCY,
                period_start=period[0],
                period_end=period[1],
                control_kind="bill_identity",
                balance_due=money(bill["total_due"]),
                due_date=_date(bill["due_date"]),
                tied_out=tied_out,
            ),
        ),
        lines=(),
        control_kind="bill_identity",
    )


PARSER: Final = register_parser(
    Parser(
        parser_id="virgin_plus.bill",
        issuer="virgin-plus",
        format="pdf",
        version="1",
        parse=parse,
    )
)
