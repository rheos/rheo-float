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
- the overdrawn spells docextract reports ride on the period.

Accounts are matched where ``accounts.csv`` gives the institution as ``INSTITUTION``,
compared by ``institution_key`` (case and spacing aside).
"""

from __future__ import annotations

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
from rheo_finance.parsers.registry import Parser, register_parser

INSTITUTION: Final = "Vancity"
CURRENCY: Final = "CAD"


def _match(
    statement: VancityStatement, accounts: AccountLookup
) -> list[tuple[VancityAccount, RegisteredAccount]]:
    period = (statement.period_start, statement.period_end)
    matched: list[tuple[VancityAccount, RegisteredAccount]] = []
    for section in statement.accounts:
        found = accounts.by_last_four(INSTITUTION, section.ref, *period)
        if not found:
            raise Rejected("unknown_account", period)
        if len(found) > 1:
            raise Rejected("ambiguous_account", period)
        matched.append((section, found[0]))
    if len({registered.id for _, registered in matched}) != len(matched):
        raise Rejected("ambiguous_account", period)
    return matched


def _line(
    registered: RegisteredAccount,
    txn: VancityTransaction,
    amount: Decimal | None,
    balance: Decimal | None,
) -> ParsedLine:
    return ParsedLine(
        "transaction" if amount is not None else "notice_fact",
        account=registered,
        posting_date=txn.date,
        amount=amount,
        currency=CURRENCY,
        description_raw=txn.description,
        running_balance=balance,
        fee_type=txn.fee_type,
    )


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
        raise Rejected("checks_failed", period)
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


PARSER: Final = register_parser(
    Parser(
        parser_id="vancity.statement",
        issuer="vancity",
        format="pdf",
        version="1",
        parse=parse,
    )
)
