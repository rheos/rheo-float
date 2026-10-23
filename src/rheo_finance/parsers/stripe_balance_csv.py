"""The Stripe balance history CSV: each payout with the charges, refunds and fees it
pays out (FR-12(a), FR-3, FR-4, FR-15(c); SPEC "Derivation" stage 1, "Fiat CSV
exports"; edge case 22).

No real export is on file yet. ``COLUMNS`` is shaped on the vendor's documented
export, to be fixed against the first real file: the balance history download with its
default columns, one row per balance transaction in either date order, ``Created
(UTC)`` as ``YYYY-MM-DD HH:MM`` (seconds optional), currency codes in either case, a
signed ``Amount`` (the gross), a ``Fee`` that is never negative, ``Net`` the amount less
the fee, and ``Transfer`` naming the payout that paid the row out. A file whose header
is not exactly ``COLUMNS`` is ``NotRecognised`` (``unparsed``, no line).

**Control kind: ``none``.** The export prints no balance, so nothing in it verifies a
line: each currency is one unverified period from its first row's date to its last's.
Should the first real file print a balance column, the parser moves to
``running_balance`` through ``parsers.balance_csv``.

A payout (``Type`` ``payout``, its id in ``Source``, a negative ``Amount``) and the rows
whose ``Transfer`` names it give, on the Stripe account, three lines dated on the
payout's created date (UTC):

- the gross: the sum of its rows' ``Amount`` (charges less refunds), a credit;
- the fees: the sum of every ``Fee`` in it, the payout's own included, as one fee line
  (``fee_type`` ``fee``), left out when zero;
- the payout: its ``Amount``, a transfer-like debit whose hints come from
  ``reference.patterns.classify`` (account kind ``payment_processor``): the Stripe side
  of ``stripe_payout``, naming the Wise account (``hint_account_slug`` ``wise``), so
  stage 9 can pair it with the Wise deposit.

So gross less fees is the payout, and gross income, fees and net are all visible. A
payout is complete when its rows' ``Net`` and its own sum to zero (it pays out exactly
what its rows put in). A payout whose rows are only partly in the file (an export cut
by date) gives its payout line alone; a fuller export later matches that line one to
one under stage 3 and adds the gross and the fee. A row whose payout is not in the
file, or that no payout has paid out yet, gives no line: the export holding its payout
carries it.

Row ids, payout ids, descriptions and the customer columns are stored nowhere (a
description can name a customer); the lines' descriptions are the parser's own words
and no line has a counterparty. A row whose ``Net`` is not ``Amount`` less ``Fee``, a
negative ``Fee``, a payout that is not a debit, two payouts with one id, or a payout
whose rows are in another currency is not this layout.

The export prints no account identifier, so the account is the one
``payment_processor`` account with a ``number`` identifier registered at
``INSTITUTION`` over the file's dates (``AccountLookup.by_institution``, which names
the residual risk): none is ``unknown_account``, several are ``ambiguous_account``.
"""

from __future__ import annotations

import datetime as dt
import re
from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal
from typing import Final

from rheo_finance.parsers import balance_csv
from rheo_finance.parsers.base import (
    AccountLookup,
    NotRecognised,
    ParsedLine,
    ParsedPeriod,
    ParseResult,
    RegisteredAccount,
    Rejected,
)
from rheo_finance.parsers.hints import with_hints
from rheo_finance.parsers.registry import Parser, register_parser

INSTITUTION: Final = "Stripe"
ISSUER: Final = "stripe"
ACCOUNT_KIND: Final = "payment_processor"
WHAT: Final = "not a Stripe balance history CSV"
# Shaped on the vendor's documented export, to be fixed against the first real file.
COLUMNS: Final = (
    "id", "Type", "Source", "Amount", "Fee", "Net", "Currency", "Created (UTC)",
    "Available On (UTC)", "Description", "Customer Facing Amount",
    "Customer Facing Currency", "Transfer", "Transfer Date (UTC)", "Transfer Group",
)  # fmt: skip
PAYOUT: Final = "payout"
GROSS_TEXT: Final = "Gross charges"
FEE_TEXT: Final = "Stripe fees"
PAYOUT_TEXT: Final = "Payout to bank"
_CREATED: Final = re.compile(r"(?P<day>\d{4}-\d{2}-\d{2}) \d{2}:\d{2}(?::\d{2})?")


@dataclass(frozen=True)
class _Row:
    kind: str
    source: str
    amount: Decimal
    fee: Decimal
    net: Decimal
    currency: str
    created: dt.datetime
    payout: str

    @property
    def day(self) -> dt.date:
        return self.created.date()


def _created(cell: str) -> dt.datetime:
    text = " ".join(cell.split())
    if _CREATED.fullmatch(text) is None:
        raise NotRecognised(WHAT)
    try:
        return dt.datetime.fromisoformat(text)
    except ValueError:
        raise NotRecognised(WHAT) from None


def _rows(text: str) -> list[_Row]:
    found: list[_Row] = []
    for cells in balance_csv.rows(text, COLUMNS, WHAT):
        amount = balance_csv.signed(cells["Amount"], WHAT)
        fee = balance_csv.signed(cells["Fee"] or "0", WHAT)
        net = balance_csv.signed(cells["Net"], WHAT)
        kind = cells["Type"].strip()
        if fee < 0 or amount - fee != net or not kind:
            raise NotRecognised(WHAT)
        found.append(
            _Row(
                kind=kind,
                source=cells["Source"].strip(),
                amount=amount,
                fee=fee,
                net=net,
                currency=balance_csv.currency(cells["Currency"].upper(), WHAT),
                created=_created(cells["Created (UTC)"]),
                payout=cells["Transfer"].strip(),
            )
        )
    if not found:
        raise NotRecognised(WHAT)
    return found


@dataclass(frozen=True)
class _Payout:
    row: _Row
    paid: tuple[_Row, ...]

    @property
    def complete(self) -> bool:
        return self.row.net + sum((each.net for each in self.paid), Decimal(0)) == 0

    def lines(self, registered: RegisteredAccount) -> list[ParsedLine]:
        def line(amount: Decimal, text: str, fee_type: str | None = None) -> ParsedLine:
            return ParsedLine(
                "transaction",
                account=registered,
                posting_date=self.row.day,
                amount=amount,
                currency=self.row.currency,
                description_raw=text,
                fee_type=fee_type,
            )

        hinted = {"account_kind": ACCOUNT_KIND, "issuer": ISSUER}
        payout = with_hints(line(self.row.amount, PAYOUT_TEXT), **hinted)
        if not self.complete:
            return [payout]
        gross = sum((each.amount for each in self.paid), Decimal(0))
        fees = self.row.fee + sum((each.fee for each in self.paid), Decimal(0))
        found = [with_hints(line(gross, GROSS_TEXT), **hinted)]
        if fees:
            found.append(line(-fees, FEE_TEXT, "fee"))
        return [*found, payout]


def _payouts(rows: list[_Row]) -> list[_Payout]:
    """Every payout in the file with the rows it pays out, oldest first."""
    by_payout: dict[str, list[_Row]] = defaultdict(list)
    heads: dict[str, _Row] = {}
    for row in rows:
        if row.kind != PAYOUT:
            if row.payout:
                by_payout[row.payout].append(row)
            continue
        if not row.source or row.source in heads or row.amount >= 0:
            raise NotRecognised(WHAT)
        heads[row.source] = row
    payouts: list[_Payout] = []
    for key, head in heads.items():
        paid = tuple(by_payout[key])
        if any(each.currency != head.currency for each in paid):
            raise NotRecognised(WHAT)
        payouts.append(_Payout(head, paid))
    return sorted(payouts, key=lambda each: each.row.created)


def _span(rows: list[_Row]) -> tuple[dt.date, dt.date]:
    days = [row.day for row in rows]
    return min(days), max(days)


def _account(accounts: AccountLookup, rows: list[_Row]) -> RegisteredAccount:
    period = _span(rows)
    found = accounts.by_institution(INSTITUTION, ACCOUNT_KIND, *period)
    if not found:
        raise Rejected("unknown_account", period)
    if len(found) > 1:
        raise Rejected("ambiguous_account", period)
    return found[0]


def parse(text: str, accounts: AccountLookup) -> ParseResult:
    rows = _rows(text)
    payouts = _payouts(rows)
    registered = _account(accounts, rows)
    by_currency: dict[str, list[_Row]] = defaultdict(list)
    for row in rows:
        by_currency[row.currency].append(row)
    periods = tuple(
        ParsedPeriod(
            account=registered,
            currency=code,
            period_start=_span(by_currency[code])[0],
            period_end=_span(by_currency[code])[1],
            control_kind="none",
        )
        for code in sorted(by_currency)
    )
    lines = tuple(line for each in payouts for line in each.lines(registered))
    return ParseResult(periods=periods, lines=lines, control_kind="none")


def filing_date(text: str) -> dt.date:
    """The latest row's date, the date the filer names a new copy by."""
    return _span(_rows(text))[1]


PARSER: Final = register_parser(
    Parser(
        parser_id="stripe.balance_csv",
        issuer=ISSUER,
        format="csv",
        version="1",
        parse=parse,
        filing_date=filing_date,
        sets_hints=True,
    )
)
