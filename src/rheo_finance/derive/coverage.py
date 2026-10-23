"""Derivation stage 6: the coverage matrix (FR-6, AC-6).

For each account, one row per calendar month from its first known date to the later
of this month (in ``finance.locale.timezone``) and its last dated record. The first
known date is the earliest of the opened date, the first document period and the first
line; it is written back to ``account.first_known_on``. An account with no opened date
and no data has no months (status shows it as "no data").

Each month takes the first state that holds:

1. ``not_applicable``: wholly before the opened date or wholly after the closed date.
2. ``have``: every day of the month the account was open is verified in every currency
   the account takes. Verified spans: a tied-out ``summary`` period (a statement), each
   continuous run of a ``running_balance`` period (``parsers.base.balance_runs`` run
   again over the stored lines, in line order), and a ``bill_identity`` period (a parsed
   bill, whose period is its bill-date month when it prints none).
3. ``rejected``: a rejected document's period on the account overlaps the month
   (``rejected_period``).
4. ``unparsed``: an ``unparsed`` or ``needs_manual_entry`` document with a known period
   on the account (from ``document_periods.csv``) overlaps the month.
5. ``partial``: some day is verified in some currency, or a ledger line is dated in the
   month (reason ``running_balance_break`` when the month holds a day strictly between
   the two lines of a running-balance break, else none), or the month overlaps the
   stated period of an unverified (control ``none``) export (reason ``export_period``).
6. ``missing``: with reason ``running_balance_break`` when the month holds a day
   strictly between the two lines of a break, else the account's ``missing_reason``
   when the month starts before its ``missing_before``, else none. So
   ``missing_before`` only names the reason for a month nothing else covers.

The built-in "Paid outside recorded accounts" (``INTERNAL_ACCOUNT``) gets no months:
it has no source by design, so every month without a cash line would read ``missing``.
Its first known date is still written.

Only ``parsed`` documents count, so a ``superseded`` copy covers nothing of its own: the
copy it repeats is the parsed one and covers the period. File names never count (only
parsed periods, stored lines and entered periods do), and neither do ``trading_export``
documents or trading lines, which carry results, not the account's movements.

A ``none``-control export verifies nothing, so a month resting on its lines alone is at
best ``partial``. The mail rules (``no_raw_mail``, ``mail_purged`` from the first parsed
email, ``mail_window``) belong to the mail intake stage, not built yet.
"""

from __future__ import annotations

import calendar
import datetime as dt
import uuid
from collections import Counter, defaultdict
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Final, Literal

from sqlalchemy import insert, select, update

from rheo_finance.context import UnitOfWork
from rheo_finance.manual.accounts import INTERNAL_ACCOUNT
from rheo_finance.manual.document_periods import DocumentPeriodRow
from rheo_finance.manual.stage2 import account_id
from rheo_finance.parsers.base import RegisteredAccount, balance_runs
from rheo_finance.storage.schema import (
    account,
    coverage,
    document,
    document_line,
    rejected_period,
    statement_period,
    txn,
)

State = Literal["have", "partial", "missing", "rejected", "unparsed", "not_applicable"]
Reason = Literal[
    "no_raw_mail", "mail_purged", "mail_window", "export_period",
    "running_balance_break",
]  # fmt: skip
STATES: Final[tuple[State, ...]] = (
    "have", "partial", "missing", "rejected", "unparsed", "not_applicable",
)  # fmt: skip

TRADING_DOCUMENT: Final = "trading_export"
UNPARSED_STATUSES: Final = ("unparsed", "needs_manual_entry")

Span = tuple[dt.date, dt.date]


@dataclass
class Facts:
    """What stage 6 knows about one account, every span inclusive."""

    currencies: frozenset[str]
    opened: dt.date | None = None
    closed: dt.date | None = None
    missing_before: dt.date | None = None
    missing_reason: Reason | None = None
    # Verified spans by currency.
    verified: dict[str, list[Span]] = field(default_factory=lambda: defaultdict(list))
    rejected: list[Span] = field(default_factory=list)
    unparsed: list[Span] = field(default_factory=list)
    export_periods: list[Span] = field(default_factory=list)
    # Running-balance breaks, from the earlier line's date to the later line's.
    breaks: list[Span] = field(default_factory=list)
    # Every document period on the account, of any kind, for the first known date.
    periods: list[Span] = field(default_factory=list)
    line_days: set[dt.date] = field(default_factory=set)


@dataclass(frozen=True)
class Month:
    month: dt.date
    state: State
    reason: Reason | None = None


@dataclass(frozen=True)
class _Days:
    """One account's facts as day sets, built once for all its months."""

    verified: dict[str, frozenset[dt.date]]
    line_months: frozenset[dt.date]
    # The days strictly between the two lines of each running-balance break.
    broken: tuple[Span, ...]


def month_start(day: dt.date) -> dt.date:
    return day.replace(day=1)


def month_end(day: dt.date) -> dt.date:
    return day.replace(day=calendar.monthrange(day.year, day.month)[1])


def _months(first: dt.date, last: dt.date) -> Iterator[dt.date]:
    current = month_start(first)
    while current <= last:
        yield current
        current = month_end(current) + dt.timedelta(days=1)


def _days(start: dt.date, end: dt.date) -> Iterator[dt.date]:
    day = start
    while day <= end:
        yield day
        day += dt.timedelta(days=1)


def _overlaps(spans: Iterable[Span], start: dt.date, end: dt.date) -> bool:
    return any(first <= end and last >= start for first, last in spans)


def _day_set(spans: Iterable[Span]) -> frozenset[dt.date]:
    return frozenset(day for first, last in spans for day in _days(first, last))


def first_known(facts: Facts) -> dt.date | None:
    """The earliest of the opened date, the first document period and the first line."""
    candidates = [start for start, _ in facts.periods]
    candidates += [min(facts.line_days)] if facts.line_days else []
    candidates += [facts.opened] if facts.opened else []
    return min(candidates, default=None)


def _month_state(facts: Facts, days: _Days, start: dt.date) -> Month:
    """The state of the month starting ``start``."""
    end = month_end(start)
    if (facts.opened and end < facts.opened) or (facts.closed and start > facts.closed):
        return Month(start, "not_applicable")
    open_days = frozenset(
        _days(max(start, facts.opened or start), min(end, facts.closed or end))
    )
    if facts.currencies and all(
        open_days <= days.verified.get(currency, frozenset())
        for currency in facts.currencies
    ):
        return Month(start, "have")
    if _overlaps(facts.rejected, start, end):
        return Month(start, "rejected")
    if _overlaps(facts.unparsed, start, end):
        return Month(start, "unparsed")
    broken = _overlaps(days.broken, start, end)
    if start in days.line_months or any(
        not open_days.isdisjoint(each) for each in days.verified.values()
    ):
        return Month(start, "partial", "running_balance_break" if broken else None)
    if _overlaps(facts.export_periods, start, end):
        return Month(start, "partial", "export_period")
    if broken:
        return Month(start, "missing", "running_balance_break")
    if facts.missing_before and start < facts.missing_before:
        return Month(start, "missing", facts.missing_reason)
    return Month(start, "missing")


def matrix(facts: Facts, today: dt.date) -> list[Month]:
    """Every month of one account, oldest first, from its first known date to the
    later of ``today``'s month and its last dated record; none when nothing is known."""
    first = first_known(facts)
    if first is None:
        return []
    last = max([today, *(end for _, end in facts.periods), *facts.line_days])
    one_day = dt.timedelta(days=1)
    days = _Days(
        verified={
            currency: _day_set(spans) for currency, spans in facts.verified.items()
        },
        line_months=frozenset(month_start(day) for day in facts.line_days),
        broken=tuple(
            (first_line + one_day, last_line - one_day)
            for first_line, last_line in facts.breaks
            if (last_line - first_line).days > 1
        ),
    )
    return [_month_state(facts, days, start) for start in _months(first, last)]


# --- reading the facts ----------------------------------------------------------------


def _accounts(uow: UnitOfWork) -> dict[uuid.UUID, Facts]:
    rows = uow.execute(
        select(
            account.c.id,
            account.c.currencies,
            account.c.opened_on,
            account.c.closed_on,
            account.c.missing_before,
            account.c.missing_reason,
        )
    )
    return {
        row.id: Facts(
            currencies=frozenset(row.currencies),
            opened=row.opened_on,
            closed=row.closed_on,
            missing_before=row.missing_before,
            missing_reason=row.missing_reason,
        )
        for row in rows
    }


def _parsed_periods(uow: UnitOfWork, facts: dict[uuid.UUID, Facts]) -> None:
    rows = uow.execute(
        select(
            statement_period.c.document_id,
            statement_period.c.account_id,
            statement_period.c.currency,
            statement_period.c.period_start,
            statement_period.c.period_end,
            statement_period.c.control_kind,
            statement_period.c.tied_out,
        )
        .join(document, document.c.id == statement_period.c.document_id)
        .where(document.c.status == "parsed", document.c.kind != TRADING_DOCUMENT)
    ).all()
    running: list[tuple[uuid.UUID, uuid.UUID, str]] = []
    for row in rows:
        each = facts.get(row.account_id)
        if each is None:
            continue
        span = (row.period_start, row.period_end)
        each.periods.append(span)
        kind = row.control_kind
        if (kind == "summary" and row.tied_out) or kind == "bill_identity":
            each.verified[row.currency].append(span)
        elif kind == "running_balance":
            running.append((row.document_id, row.account_id, row.currency))
        elif kind == "none":
            each.export_periods.append(span)
    for document_id, account_ref, currency in running:
        _running_balance(uow, facts[account_ref], document_id, account_ref, currency)


def _running_balance(
    uow: UnitOfWork,
    facts: Facts,
    document_id: uuid.UUID,
    account_ref: uuid.UUID,
    currency: str,
) -> None:
    """The verified segments and breaks of one running-balance period, from its stored
    lines in line order. A line without an amount or a balance cannot continue a run,
    so it is left out and the lines either side are compared directly."""
    rows = uow.execute(
        select(
            document_line.c.posting_date,
            document_line.c.amount,
            document_line.c.running_balance,
        )
        .where(
            document_line.c.document_id == document_id,
            document_line.c.account_id == account_ref,
            document_line.c.currency == currency,
            document_line.c.line_kind == "transaction",
        )
        .order_by(document_line.c.line_no)
    )
    triples: list[tuple[dt.date, Decimal, Decimal]] = [
        (row.posting_date, row.amount, row.running_balance)
        for row in rows
        if row.posting_date is not None
        and row.amount is not None
        and row.running_balance is not None
    ]
    segments, gaps = balance_runs(RegisteredAccount(account_ref, ""), triples)
    facts.verified[currency].extend(
        (each.start_date, each.end_date) for each in segments
    )
    facts.breaks.extend((each.start_date, each.end_date) for each in gaps)


def _rejected(uow: UnitOfWork, facts: dict[uuid.UUID, Facts]) -> None:
    rows = uow.execute(
        select(
            rejected_period.c.account_id,
            rejected_period.c.period_start,
            rejected_period.c.period_end,
        )
        .join(document, document.c.id == rejected_period.c.document_id)
        .where(document.c.status == "rejected", document.c.kind != TRADING_DOCUMENT)
    )
    for row in rows:
        if (each := facts.get(row.account_id)) is not None:
            span = (row.period_start, row.period_end)
            each.rejected.append(span)
            each.periods.append(span)


def _unparsed(
    uow: UnitOfWork,
    facts: dict[uuid.UUID, Facts],
    known: Sequence[DocumentPeriodRow],
) -> None:
    """Entered periods of documents no parser reads. A row whose document is not
    registered, or is no longer unparsed, counts for nothing."""
    by_path: dict[str, list[DocumentPeriodRow]] = defaultdict(list)
    for row in known:
        by_path[row.archive_path].append(row)
    if not by_path:
        return
    paths = uow.scalars(
        select(document.c.path).where(
            document.c.path.in_(list(by_path)),
            document.c.status.in_(UNPARSED_STATUSES),
            document.c.kind != TRADING_DOCUMENT,
        )
    ).all()
    for path in paths:
        for row in by_path[path]:
            if (each := facts.get(account_id(row.account))) is not None:
                span = (row.period_start, row.period_end)
                each.unparsed.append(span)
                each.periods.append(span)


def _lines(uow: UnitOfWork, facts: dict[uuid.UUID, Facts]) -> None:
    rows = uow.execute(
        select(txn.c.account_id, txn.c.posting_date)
        .distinct()
        .where(txn.c.origin != TRADING_DOCUMENT, txn.c.movement_kind != "trading")
    )
    for row in rows:
        if (each := facts.get(row.account_id)) is not None:
            each.line_days.add(row.posting_date)


def build_coverage(
    uow: UnitOfWork, known_periods: Sequence[DocumentPeriodRow], today: dt.date
) -> Counter[State]:
    """Write every account's months and first known date; the count of months by
    state. ``known_periods`` are the ``document_periods.csv`` rows stage 2 loaded;
    ``today`` is the local date."""
    facts = _accounts(uow)
    _parsed_periods(uow, facts)
    _rejected(uow, facts)
    _unparsed(uow, facts, known_periods)
    _lines(uow, facts)

    internal = account_id(INTERNAL_ACCOUNT.slug)
    rows: list[dict[str, object]] = []
    counts: Counter[State] = Counter()
    for account_ref in sorted(facts):
        each = facts[account_ref]
        uow.execute(
            update(account)
            .where(account.c.id == account_ref)
            .values(first_known_on=first_known(each))
        )
        if account_ref == internal:
            continue
        for month in matrix(each, today):
            counts[month.state] += 1
            rows.append(
                {
                    "account_id": account_ref,
                    "month": month.month,
                    "state": month.state,
                    "reason_code": month.reason,
                }
            )
    if rows:
        uow.execute(insert(coverage), rows)
    return counts
