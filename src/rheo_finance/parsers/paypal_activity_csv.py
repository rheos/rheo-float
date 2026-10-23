"""The PayPal activity CSV (FR-12(a), FR-3, FR-4, FR-15(c); SPEC "Derivation" stage 1,
"Fiat CSV exports").

No real export is on file yet. ``COLUMNS`` is shaped on the vendor's documented
export, to be fixed against the first real file: the activity download with every
column, dates ``DD/MM/YYYY`` as the activity statement prints them, signed amounts,
and a ``Balance`` and a ``Balance Impact`` column. A file whose header is not exactly
``COLUMNS`` is ``NotRecognised`` (``unparsed``, no line).

**Control kind: ``running_balance``, per currency.** ``Balance`` is the row's
currency's balance after the row (``parsers.balance_csv``). A ``Memo`` row (an
authorization, a pending hold) moves no balance and gives no line; a ``Debit`` or
``Credit`` row whose net has the other sign is not this layout.

The lines are the activity statement's (``parsers.paypal_activity``), so stage 3
pairs the two one to one when they overlap: the matcher's key is account, currency,
signed amount and a date inside the tolerance, and each row gives

- a line of its gross in its own currency (a currency conversion is a row in each
  currency, each its own leg), and
- a fee line of the fee as printed when it is not zero (``fee_type`` ``fee``), with
  the statement's description plus `` (fee)``; gross plus fee must be the net.

The description is the type and the name, as the statement prints kind and name, and
the name alone is the restricted ``counterparty``. Emails, transaction ids, addresses,
phone numbers, item and invoice fields are stored nowhere. Hints come from
``reference.patterns.classify`` (account kind ``payment_processor``) on every line but
the fee lines: "General Currency Conversion" carries ``conversion_hint``, "Bank Deposit
to PP Account" its ``paypal_preauthorized`` transfer hint.

The export prints no account identifier, so the account is the one
``payment_processor`` account with a ``number`` identifier registered at
``INSTITUTION`` over the file's dates (``AccountLookup.by_institution``, which names
the residual risk): none is ``unknown_account``, several are ``ambiguous_account``.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable
from typing import Final

from rheo_finance.parsers import balance_csv
from rheo_finance.parsers.base import (
    AccountLookup,
    NotRecognised,
    ParsedLine,
    ParseResult,
    RegisteredAccount,
)
from rheo_finance.parsers.card import written_date
from rheo_finance.parsers.hints import with_hints
from rheo_finance.parsers.paypal_activity import ACCOUNT_KIND, INSTITUTION, ISSUER
from rheo_finance.parsers.registry import Parser, register_parser

WHAT: Final = "not a PayPal activity CSV"
# Shaped on the vendor's documented export, to be fixed against the first real file.
COLUMNS: Final = (
    "Date", "Time", "TimeZone", "Name", "Type", "Status", "Currency", "Gross", "Fee",
    "Net", "From Email Address", "To Email Address", "Transaction ID",
    "Shipping Address", "Address Status", "Item Title", "Item ID",
    "Shipping and Handling Amount", "Insurance Amount", "Sales Tax", "Option 1 Name",
    "Option 1 Value", "Option 2 Name", "Option 2 Value", "Reference Txn ID",
    "Invoice Number", "Custom Number", "Quantity", "Receipt ID", "Balance",
    "Address Line 1", "Address Line 2/District/Neighborhood", "Town/City",
    "State/Province/Region/County/Territory/Prefecture/Republic", "Zip/Postal Code",
    "Country", "Contact Phone Number", "Subject", "Note", "Country Code",
    "Balance Impact",
)  # fmt: skip


def _lines(
    day: dt.date, code: str, cells: dict[str, str]
) -> Callable[[RegisteredAccount], tuple[ParsedLine, ...]]:
    gross = balance_csv.signed(cells["Gross"], WHAT)
    fee = balance_csv.signed(cells["Fee"] or "0", WHAT)
    if gross + fee != balance_csv.signed(cells["Net"], WHAT):
        raise NotRecognised(WHAT)
    kind = " ".join(cells["Type"].split())
    name = " ".join(cells["Name"].split())
    if not kind:
        raise NotRecognised(WHAT)
    description = f"{kind} {name}".strip()

    def make(registered: RegisteredAccount) -> tuple[ParsedLine, ...]:
        line = ParsedLine(
            "transaction",
            account=registered,
            posting_date=day,
            amount=gross,
            currency=code,
            description_raw=description,
            counterparty=name or None,
        )
        found = [with_hints(line, account_kind=ACCOUNT_KIND, issuer=ISSUER)]
        if fee:
            found.append(
                ParsedLine(
                    "transaction",
                    account=registered,
                    posting_date=day,
                    amount=fee,
                    currency=code,
                    description_raw=f"{description} (fee)",
                    fee_type="fee",
                )
            )
        return tuple(found)

    return make


def _movements(text: str) -> list[balance_csv.Movement]:
    movements: list[balance_csv.Movement] = []
    for cells in balance_csv.rows(text, COLUMNS, WHAT):
        impact = cells["Balance Impact"].strip()
        if impact == "Memo":
            continue
        net = balance_csv.signed(cells["Net"], WHAT)
        wrong_sign = net > 0 if impact == "Debit" else net < 0
        if impact not in ("Debit", "Credit") or wrong_sign:
            raise NotRecognised(WHAT)
        day = written_date(cells["Date"], "%d/%m/%Y")
        code = balance_csv.currency(cells["Currency"], WHAT)
        movements.append(
            balance_csv.Movement(
                day=day,
                currency=code,
                balance=balance_csv.signed(cells["Balance"], WHAT),
                lines=_lines(day, code, cells),
            )
        )
    if not movements:
        raise NotRecognised(WHAT)
    return movements


def parse(text: str, accounts: AccountLookup) -> ParseResult:
    movements = _movements(text)
    registered = balance_csv.one_account(accounts, INSTITUTION, ACCOUNT_KIND, movements)
    return balance_csv.running_balance_result(registered, movements, WHAT)


def filing_date(text: str) -> dt.date:
    """The latest row's date, the date the filer names a new copy by."""
    return balance_csv.span(_movements(text))[1]


PARSER: Final = register_parser(
    Parser(
        parser_id="paypal.activity_csv",
        issuer=ISSUER,
        format="csv",
        version="1",
        parse=parse,
        filing_date=filing_date,
        sets_hints=True,
    )
)
