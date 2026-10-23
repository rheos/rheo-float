"""The PayPal activity statement PDF (FR-8, FR-15(c), Derivation stage 1).

**Control kind: ``summary`` per currency.** Decided against the real file, as stage 1
asks: its Balance Summary prints, for each currency (CAD and USD), the available
balance at the beginning and at the end of the statement period. So each currency
is one ``statement_period`` with that opening and closing, and the registry ties its
lines out to the cent (the statement prints no debit or credit totals to check).
``running_balance`` would need a balance per line, which the statement does not
print.

The layout, as pdftotext prints it: a page header ``<kind> Account ID: <id>   PayPal
ID: <email>   DD/MM/YYYY - DD/MM/YYYY``, ``Activity Statement for <D Month YYYY> to
<D Month YYYY>``, the Balance Summary rows ``<CUR>  <available beginning>  <available
ending>  <other beginning>  <other ending>``, summaries by kind, then one
``Transaction History - <CUR>`` section per currency. A transaction there is a group of
lines between blank lines: its kind and the counterpart's name (in the order the
layout wraps them), one date line ``DD/MM/YYYY ... <gross> <fee> <net>``, an
``ID:`` line and often the counterpart's email.

- Each transaction gives a line of its gross in the section's currency, and a fee line
  when the fee is not zero (``fee_type`` ``fee``); gross plus fee must be the net, or
  the text is not this layout.
- The description is the kind and the name; the name alone is the restricted
  ``counterparty``. The ``ID:`` line and every email are stored nowhere.
- Hints from ``reference.patterns.classify`` (account kind ``payment_processor``) on
  every line but the fee lines: a "General Currency Conversion" pair (one leg per
  currency section) carries ``conversion_hint``, and "Bank Deposit to PP Account",
  the bank funding a pre-authorized payment, its ``paypal_preauthorized`` transfer
  hint.
- The account is matched by the printed Account ID among the registry's ``number``
  identifiers at ``INSTITUTION`` (letters and digits compared); it is stored nowhere.
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
    Rejected,
)
from rheo_finance.parsers.card import AMOUNT_TEXT, written_date
from rheo_finance.parsers.hints import with_hints
from rheo_finance.parsers.registry import Parser, register_parser

INSTITUTION: Final = "PayPal"
ISSUER: Final = "paypal"
ACCOUNT_KIND: Final = "payment_processor"
# The transaction kinds the statement prints, by how they start: the line that starts
# with one is the kind, the other text lines of the transaction are the name.
KINDS: Final = (
    "Bank Deposit to PP Account",
    "Express Checkout Payment",
    "General Currency Conversion",
    "Mass Pay Payment",
    "Payment Refund",
    "PreApproved Payment",
    "User Initiated Withdrawal",
)

_SIGNED: Final = rf"-?(?:{AMOUNT_TEXT})"
_HEADER: Final = re.compile(r"\w+ Account ID: (?P<id>[A-Z0-9]+)\s{2,}PayPal ID:.*")
_PERIOD: Final = re.compile(
    r"Activity Statement for (?P<start>\d{1,2} [A-Z][a-z]+ \d{4})"
    r" to (?P<end>\d{1,2} [A-Z][a-z]+ \d{4})\s*"
)
_BALANCE: Final = re.compile(
    rf"\s*(?P<currency>[A-Z]{{3}})\s+(?P<opening>{_SIGNED})\s+(?P<closing>{_SIGNED})"
    rf"\s+{_SIGNED}\s+{_SIGNED}\s*"
)
_SECTION: Final = re.compile(r"\s*Transaction History - (?P<currency>[A-Z]{3})\s*")
_DATED: Final = re.compile(
    rf"\s*(?P<day>\d\d/\d\d/\d{{4}})(?P<middle>.*?)\s+(?P<gross>{_SIGNED})"
    rf"\s+(?P<fee>{_SIGNED})\s+(?P<net>{_SIGNED})\s*"
)
_DAY: Final = re.compile(r"\s*\d\d/\d\d/\d{4}\b.*")
_WORD: Final = re.compile(r"[A-Z][a-z]+")


def _money(text: str) -> Decimal:
    return Decimal(text.replace(",", ""))


@dataclass(frozen=True)
class _Row:
    currency: str
    day: dt.date
    kind: str
    name: str
    gross: Decimal
    fee: Decimal


def _texts(lines: list[str]) -> list[str]:
    """A transaction's text lines: no ``ID:`` line, no email."""
    kept = []
    for line in lines:
        words = [word for word in line.split() if "@" not in word]
        if line.strip().startswith("ID:") or not words:
            continue
        kept.append(" ".join(words) if len(words) < len(line.split()) else line.strip())
    return kept


def _row(currency: str, group: list[str]) -> _Row | None:
    dated = [m for m in map(_DATED.fullmatch, group) if m is not None]
    if len(dated) != 1:
        if any(_DAY.fullmatch(line) for line in group):
            raise NotRecognised("a PayPal transaction this layout does not print")
        return None  # a page header, the column header or the closing notes
    match = dated[0]
    gross, fee, net = (_money(match[key]) for key in ("gross", "fee", "net"))
    if gross + fee != net:
        raise NotRecognised("a PayPal transaction whose net is not gross plus fee")
    at = group.index(match.string)
    texts = _texts(group[:at])
    # The date line's own words continue the kind ("Payment"); the rest is email.
    tail = " ".join(_WORD.findall(match["middle"]))
    kinds = [text for text in texts if text.startswith(KINDS)]
    if kinds:
        first = kinds[0]
        cells = re.split(r"\s{2,}", first)
        kind = " ".join([cells[0], tail]).strip()
        names = [*cells[1:], *(text for text in texts if text is not first)]
    else:
        cells = re.split(r"\s{2,}", texts[0]) if texts else [""]
        kind = " ".join([cells[0], tail]).strip()
        names = [*cells[1:], *texts[1:]]
    if not kind:
        raise NotRecognised("a PayPal transaction without its kind")
    return _Row(
        currency=currency,
        day=written_date(match["day"], "%d/%m/%Y"),
        kind=kind,
        name=" ".join(" ".join(names).split()),
        gross=gross,
        fee=fee,
    )


def _rows(lines: list[str]) -> list[_Row]:
    rows: list[_Row] = []
    currency: str | None = None
    group: list[str] = []

    def close() -> None:
        if currency is not None and group:
            row = _row(currency, group)
            if row is not None:
                rows.append(row)
        group.clear()

    for line in lines:
        section = _SECTION.fullmatch(line)
        if section is not None:
            close()
            currency = section["currency"]
        elif not line.strip():
            close()
        else:
            group.append(line)
    close()
    return rows


@dataclass(frozen=True)
class _Statement:
    account_id: str
    period: tuple[dt.date, dt.date]
    balances: dict[str, tuple[Decimal, Decimal]]
    rows: list[_Row]


def _read(text: str) -> _Statement:
    lines = text.splitlines()
    ids = {m["id"] for m in map(_HEADER.fullmatch, lines) if m is not None}
    periods = {(m["start"], m["end"]) for m in map(_PERIOD.fullmatch, lines) if m}
    if len(ids) != 1 or len(periods) != 1:
        raise NotRecognised("not a PayPal activity statement")
    ((start, end),) = periods
    balances: dict[str, tuple[Decimal, Decimal]] = {}
    for match in map(_BALANCE.fullmatch, lines):
        if match is not None:
            if match["currency"] in balances:
                raise NotRecognised("a currency with two balance rows")
            balances[match["currency"]] = (
                _money(match["opening"]), _money(match["closing"]),
            )  # fmt: skip
    if not balances:
        raise NotRecognised("a PayPal statement without its balance summary")
    return _Statement(
        account_id=ids.pop(),
        period=(written_date(start, "%d %B %Y"), written_date(end, "%d %B %Y")),
        balances=balances,
        rows=_rows(lines),
    )


def parse(text: str, accounts: AccountLookup) -> ParseResult:
    statement = _read(text)
    period = statement.period
    found = accounts.by_identifier(INSTITUTION, statement.account_id, *period)
    if not found:
        raise Rejected("unknown_account", period)
    if len(found) > 1:
        raise Rejected("ambiguous_account", period)
    registered = found[0]
    periods = tuple(
        ParsedPeriod(
            account=registered,
            currency=currency,
            period_start=period[0],
            period_end=period[1],
            control_kind="summary",
            opening=opening,
            closing=closing,
        )
        for currency, (opening, closing) in statement.balances.items()
    )
    lines: list[ParsedLine] = []
    for row in sorted(statement.rows, key=lambda each: each.day):
        description = f"{row.kind} {row.name}".strip()
        line = ParsedLine(
            "transaction",
            account=registered,
            posting_date=row.day,
            amount=row.gross,
            currency=row.currency,
            description_raw=description,
            counterparty=row.name or None,
        )
        lines.append(with_hints(line, account_kind=ACCOUNT_KIND, issuer=ISSUER))
        if row.fee:
            lines.append(
                ParsedLine(
                    "transaction",
                    account=registered,
                    posting_date=row.day,
                    amount=row.fee,
                    currency=row.currency,
                    description_raw=f"{description} (fee)",
                    fee_type="fee",
                )
            )
    return ParseResult(periods=periods, lines=tuple(lines), control_kind="summary")


def filing_date(text: str) -> dt.date:
    """The statement period's end, the date the filer names a new copy by."""
    return _read(text).period[1]


PARSER: Final = register_parser(
    Parser(
        parser_id="paypal.activity",
        issuer=ISSUER,
        format="pdf",
        version="1",
        parse=parse,
        filing_date=filing_date,
        sets_hints=True,
    )
)
