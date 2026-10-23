"""The Vancity monthly statement (PDF), through ``docextract`` (FR-7, Key decision 1).

docextract reads the layout and checks every account against the statement's summary
box; this module only maps its result onto the ledger:

- a non-empty ``checks`` list rejects the whole document (``checks_failed``);
- each account section becomes one CAD ``statement_period`` with control kind
  ``summary``, matched to the registry by its ``ref`` (the account number's last four
  digits) against the identifiers of this institution, never by the printed name: an
  account renamed on later statements keeps its number and its match. An account
  the registry does not hold rejects the document (``unknown_account``); two
  registered accounts for one ref, or two sections for one account, reject it as
  ``ambiguous_account``;
- each transaction becomes a line with its running balance and ``fee_type``. A line
  holding both a withdrawal and a deposit becomes two lines, the withdrawal first; a
  line moving no money (a notice) is a ``notice_fact`` with no amount;
- the overdrawn spells docextract reports ride on the period;
- every line carries the hints its wording gives (``parsers.hints.with_hints``), and
  a bill payment or pre-authorized payment keeps the payee it prints in parentheses
  as its restricted ``counterparty`` (an e-Transfer's name comes from ``with_hints``).

Accounts are matched where ``accounts.csv`` gives the institution as ``INSTITUTION``,
compared by ``institution_key`` (case and spacing aside).
"""

from __future__ import annotations

import datetime as dt
import re
from decimal import Decimal
from typing import Final

from rheo_finance.parsers.base import (
    AccountLookup,
    OverdrawnSpell,
    ParsedLine,
    ParsedPeriod,
    ParseResult,
    RegisteredAccount,
    Rejected,
)
from rheo_finance.parsers.docextract_adapter import (
    VancityAccount,
    VancityStatement,
    VancityTransaction,
    vancity_statement,
)
from rheo_finance.parsers.hints import with_hints
from rheo_finance.parsers.registry import Parser, register_parser

INSTITUTION: Final = "Vancity"
ISSUER: Final = "vancity"
CURRENCY: Final = "CAD"
# shortcut: every section is classified as a chequing account. A Vancity statement
# holds deposit accounts only and every built-in pattern scopes chequing and savings
# together; pass the registered kind once a pattern tells the two apart.
ACCOUNT_KIND: Final = "chequing"
# "BILL PAYMENT-ONLINE (<payee> #<ref>)", "PREAUTHORIZED PAYMENT (<payee>)": the
# payee runs to the last closing parenthesis (a payee may hold its own).
_PAYEE: Final = re.compile(
    r"(?:BILL PAYMENT\S*|PREAUTHORIZED PAYMENT)\s*\((?P<payee>.+)\)\s*", re.IGNORECASE
)
_REFERENCE: Final = re.compile(r"\s*#\s*\d+$")


def payee(description: str) -> str | None:
    """The payee a bill or pre-authorized payment prints, without its reference."""
    match = _PAYEE.fullmatch(" ".join(description.split()))
    if match is None:
        return None
    return _REFERENCE.sub("", match["payee"]).strip() or None


def _match(
    statement: VancityStatement, accounts: AccountLookup
) -> list[tuple[VancityAccount, RegisteredAccount]]:
    """Each section with its registered account. A rejection names the accounts that
    did match one section each, so coverage shows their months as rejected."""
    period = (statement.period_start, statement.period_end)
    found = [
        (section, accounts.by_last_four(INSTITUTION, section.ref, *period))
        for section in statement.accounts
    ]
    named = tuple(dict.fromkeys(each[0] for _, each in found if len(each) == 1))
    # The first section that fails decides the reason, as when matching stopped there.
    for _, each in found:
        if not each:
            raise Rejected("unknown_account", period, named)
        if len(each) > 1:
            raise Rejected("ambiguous_account", period, named)
    if len(named) != len(found):
        raise Rejected("ambiguous_account", period, named)
    return [(section, each[0]) for section, each in found]


def _line(
    registered: RegisteredAccount,
    txn: VancityTransaction,
    amount: Decimal | None,
    balance: Decimal | None,
) -> ParsedLine:
    line = ParsedLine(
        "transaction" if amount is not None else "notice_fact",
        account=registered,
        posting_date=txn.date,
        amount=amount,
        currency=CURRENCY,
        description_raw=txn.description,
        counterparty=payee(txn.description),
        running_balance=balance,
        fee_type=txn.fee_type,
    )
    return with_hints(line, account_kind=ACCOUNT_KIND, issuer=ISSUER)


def _lines(section: VancityAccount, registered: RegisteredAccount) -> list[ParsedLine]:
    lines: list[ParsedLine] = []
    previous = section.opening
    for txn in section.transactions:
        if txn.withdrawal is not None and txn.deposit is not None:
            # Both on one printed line: the balance between them is not printed.
            between = previous - txn.withdrawal
            lines.append(_line(registered, txn, -txn.withdrawal, between))
            lines.append(_line(registered, txn, txn.deposit, txn.balance))
        elif txn.withdrawal is not None:
            lines.append(_line(registered, txn, -txn.withdrawal, txn.balance))
        else:
            lines.append(_line(registered, txn, txn.deposit, txn.balance))
        if txn.balance is not None:
            previous = txn.balance
    return lines


def parse(text: str, accounts: AccountLookup) -> ParseResult:
    statement = vancity_statement(text)
    period = (statement.period_start, statement.period_end)
    if statement.failed_checks:
        # The reason stays checks_failed whatever the accounts say; the accounts that
        # match are named, for coverage.
        try:
            named = tuple(registered for _, registered in _match(statement, accounts))
        except Rejected as unmatched:
            named = unmatched.accounts
        raise Rejected("checks_failed", period, named)
    periods: list[ParsedPeriod] = []
    lines: list[ParsedLine] = []
    for section, registered in _match(statement, accounts):
        periods.append(
            ParsedPeriod(
                account=registered,
                currency=CURRENCY,
                period_start=statement.period_start,
                period_end=statement.period_end,
                control_kind="summary",
                opening=section.opening,
                closing=section.closing,
                total_debits=section.withdrawals,
                total_credits=section.deposits,
                overdrawn=tuple(
                    OverdrawnSpell(each.start, each.end) for each in section.overdrawn
                ),
            )
        )
        lines += _lines(section, registered)
    return ParseResult(
        periods=tuple(periods), lines=tuple(lines), control_kind="summary"
    )


def filing_date(text: str) -> dt.date:
    """The statement's period end, the date the filer names a new copy by."""
    return vancity_statement(text).period_end


PARSER: Final = register_parser(
    Parser(
        parser_id="vancity.statement",
        issuer=ISSUER,
        format="pdf",
        # 2: the lines carry hints and the printed payee.
        version="2",
        parse=parse,
        filing_date=filing_date,
        sets_hints=True,
    )
)
