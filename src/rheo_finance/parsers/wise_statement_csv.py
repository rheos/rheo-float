"""The Wise statement CSV (FR-12(a), FR-3, FR-4, FR-15(c); SPEC "Derivation" stage 1,
"Fiat CSV exports").

No real export is on file yet. ``COLUMNS`` is shaped on the vendor's documented
export, to be fixed against the first real file: one row per transaction of one or
more balances, a ``TransferWise ID`` of the form ``<KIND>-<number>``, dates
``DD-MM-YYYY``, a signed ``Amount`` that is the row's change of balance with its fees
included, ``Total fees`` as a positive figure, and a ``Running Balance``. A file whose
header is not exactly ``COLUMNS`` is ``NotRecognised`` (``unparsed``, no line).

**Control kind: ``running_balance``, per currency** (``parsers.balance_csv``). Unlike
the transaction history PDF, the export prints a balance, so its lines are verified
where the balances continue, and the document is the vendor's own statement
(``official``).

The lines are the history PDF's (``parsers.wise_history``), so stage 3 pairs the two
one to one when they overlap: the matcher's key is account, currency, signed amount
and a date inside the tolerance, and each row gives

- a principal line of the amount less the fee (for money out, what left net of the
  fee; a card or transfer paid in another currency keeps that amount, signed, as
  ``original_amount``), and
- a fee line of the fee, negative, when it is not zero: ``fee_type``
  ``conversion_fee`` when the row exchanges between two currencies, else ``fee``.

A conversion (kind ``BALANCE``) is a row in each balance: the out-leg in the source
currency carries the fee, the in-leg in the target currency is what arrived, so the
fee line is the conversion's only one, as on the PDF. A row that moves nothing (a card
check) gives no line.

The description is the export's own, and carries the hints from
``reference.patterns.classify`` (account kind ``wallet``) on every line but the fee
lines: "Converted" marks the conversion legs, "Stripe" a payout from Stripe with its
``stripe_payout`` hint naming the Stripe account. A transfer's payer (money in) or
payee (money out) is its restricted ``counterparty``; a card merchant is not a
counterparty. The ``TransferWise ID`` and the payment reference, payee account
number, card digits, card holder, attachment and note columns are stored nowhere (the
description is kept as the export prints it).

The export prints no account identifier, so the account is the one registered at
``INSTITUTION`` over the file's dates (``AccountLookup.by_institution``): none is
``unknown_account``, several are ``ambiguous_account``.
"""

from __future__ import annotations

import datetime as dt
import re
from collections.abc import Callable
from decimal import Decimal
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
from rheo_finance.parsers.registry import Parser, register_parser
from rheo_finance.parsers.wise_history import ACCOUNT_KIND, INSTITUTION, ISSUER

WHAT: Final = "not a Wise statement CSV"
# Shaped on the vendor's documented export, to be fixed against the first real file.
COLUMNS: Final = (
    "TransferWise ID", "Date", "Amount", "Currency", "Description",
    "Payment Reference", "Running Balance", "Exchange From", "Exchange To",
    "Exchange Rate", "Payer Name", "Payee Name", "Payee Account Number", "Merchant",
    "Card Last Four Digits", "Card Holder Full Name", "Attachment", "Note",
    "Total fees", "Exchange To Amount",
)  # fmt: skip
CONVERSION: Final = "BALANCE"
CARD: Final = "CARD"
_ID: Final = re.compile(r"(?P<kind>[A-Z][A-Z_]*)-\d+")


def _plain(cell: str) -> str:
    return " ".join(cell.split())


def _lines(
    kind: str, day: dt.date, code: str, cells: dict[str, str]
) -> Callable[[RegisteredAccount], tuple[ParsedLine, ...]]:
    amount = balance_csv.signed(cells["Amount"], WHAT)
    fee = balance_csv.signed(cells["Total fees"] or "0", WHAT)
    if fee < 0:
        raise NotRecognised(WHAT)
    source, target = _plain(cells["Exchange From"]), _plain(cells["Exchange To"])
    crossed = bool(source and target) and source != target
    original: tuple[Decimal, str] | None = None
    if kind == CONVERSION:
        if not crossed or code not in (source, target):
            raise NotRecognised(WHAT)
        counterparty = None
    else:
        if crossed and amount < 0:
            arrived = balance_csv.signed(cells["Exchange To Amount"], WHAT)
            original = (-abs(arrived), balance_csv.currency(target, WHAT))
        named = cells["Payee Name"] if amount < 0 else cells["Payer Name"]
        counterparty = None if kind == CARD else _plain(named) or None
    description = _plain(cells["Description"])
    if not description:
        raise NotRecognised(WHAT)

    def make(registered: RegisteredAccount) -> tuple[ParsedLine, ...]:
        line = ParsedLine(
            "transaction",
            account=registered,
            posting_date=day,
            amount=amount + fee,
            currency=code,
            original_amount=None if original is None else original[0],
            original_currency=None if original is None else original[1],
            description_raw=description,
            counterparty=counterparty,
        )
        found = [with_hints(line, account_kind=ACCOUNT_KIND, issuer=ISSUER)]
        if fee:
            found.append(
                ParsedLine(
                    "transaction",
                    account=registered,
                    posting_date=day,
                    amount=-fee,
                    currency=code,
                    description_raw=f"{description} (fee)",
                    fee_type="conversion_fee" if crossed else "fee",
                )
            )
        return tuple(found)

    return make


def _movements(text: str) -> list[balance_csv.Movement]:
    movements: list[balance_csv.Movement] = []
    for cells in balance_csv.rows(text, COLUMNS, WHAT):
        identity = _ID.fullmatch(cells["TransferWise ID"].strip())
        if identity is None:
            raise NotRecognised(WHAT)
        amount = balance_csv.signed(cells["Amount"], WHAT)
        if not amount and not balance_csv.signed(cells["Total fees"] or "0", WHAT):
            continue  # a card check: nothing moved
        day = written_date(cells["Date"], "%d-%m-%Y")
        code = balance_csv.currency(cells["Currency"], WHAT)
        movements.append(
            balance_csv.Movement(
                day=day,
                currency=code,
                balance=balance_csv.signed(cells["Running Balance"], WHAT),
                lines=_lines(identity["kind"], day, code, cells),
            )
        )
    if not movements:
        raise NotRecognised(WHAT)
    return movements


def parse(text: str, accounts: AccountLookup) -> ParseResult:
    movements = _movements(text)
    registered = balance_csv.one_account(accounts, INSTITUTION, movements)
    return balance_csv.running_balance_result(registered, movements, WHAT)


def filing_date(text: str) -> dt.date:
    """The latest row's date, the date the filer names a new copy by."""
    return balance_csv.span(_movements(text))[1]


PARSER: Final = register_parser(
    Parser(
        parser_id="wise.statement_csv",
        issuer=ISSUER,
        format="csv",
        version="1",
        parse=parse,
        filing_date=filing_date,
        sets_hints=True,
    )
)
