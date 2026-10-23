"""Document precedence: how parsed documents rank on each account (SPEC "Derivation"
stages 1 and 3, Key decision 4).

A document ranks on an account by its control there, then its format, then its sha256:

- control: a tied-out ``summary`` period (a statement), then a ``running_balance``
  period (a verified export), then anything else (an unverified export, including a
  document that has lines on the account and no period for it);
- format: a statement PDF (a PDF with a summary period), a transaction PDF (any other
  PDF), a CSV, then anything else (an API response and the like);
- sha256: the larger one first, so that of two documents equal on both, the one with
  the larger sha256 is primary.

Every input is stored parse output, so the order is a function of the document set and
never of the order the files were imported in.

``supersede`` is the stage 1 half: of two or more parsed documents that rank equally
on every account they cover, for exactly the same periods, with the same transaction
lines, the one with the larger sha256 stays ``parsed`` and the others become
``superseded``. The lines are compared too: two exports can state the same period and
still differ inside it, and superseding one would drop its lines without a trace.
Documents that differ in lines, or share a period on only some of their accounts, are
left to stage 3's matcher, which records the repeated lines as additional sources and
keeps the rest.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Final, Literal

from sqlalchemy import select, update

from rheo_finance.context import UnitOfWork
from rheo_finance.storage.schema import document, document_line, statement_period
from rheo_finance.text import normalize_description

Basis = Literal["statement", "verified_export", "unverified_export"]

STATEMENT: Final = 0
VERIFIED_EXPORT: Final = 1
UNVERIFIED_EXPORT: Final = 2
BASIS: Final[Mapping[int, Basis]] = {
    STATEMENT: "statement",
    VERIFIED_EXPORT: "verified_export",
    UNVERIFIED_EXPORT: "unverified_export",
}

Period = tuple[str, dt.date, dt.date]


@dataclass(frozen=True)
class Standing:
    """One parsed document's place on one account."""

    document_id: uuid.UUID
    account_id: uuid.UUID
    control: int
    format_rank: int
    sha256: str
    # (currency, start, end) of every period the document states for the account.
    periods: frozenset[Period]

    @property
    def windows(self) -> tuple[tuple[dt.date, dt.date], ...]:
        """The periods this document owns on the account: its tied-out statement
        periods, none for an export."""
        if self.control != STATEMENT:
            return ()
        return tuple(sorted({(start, end) for _, start, end in self.periods}))

    @property
    def basis(self) -> Basis:
        return BASIS[self.control]


@dataclass
class _Facts:
    """What the parse cache says about one document, by account."""

    sha256: str
    fmt: str
    summary: bool = False
    controls: dict[uuid.UUID, set[tuple[str, bool]]] = field(default_factory=dict)
    periods: dict[uuid.UUID, set[Period]] = field(default_factory=dict)


def _control(kinds: Iterable[tuple[str, bool]]) -> int:
    kinds = set(kinds)
    if ("summary", True) in kinds:
        return STATEMENT
    if any(kind == "running_balance" for kind, _ in kinds):
        return VERIFIED_EXPORT
    return UNVERIFIED_EXPORT


def _format_rank(fmt: str, summary: bool) -> int:
    if fmt == "pdf":
        return 0 if summary else 1
    if fmt == "csv":
        return 2
    return 3


def ranked(standings: Iterable[Standing]) -> list[Standing]:
    """``standings`` in precedence order: control, format, then the larger sha256."""
    by_sha = sorted(standings, key=lambda each: each.sha256, reverse=True)
    return sorted(by_sha, key=lambda each: (each.control, each.format_rank))


def standings(uow: UnitOfWork) -> dict[uuid.UUID, list[Standing]]:
    """Every parsed document's standing on each account it has periods or transaction
    lines on, by account, in precedence order."""
    facts: dict[uuid.UUID, _Facts] = {}

    def fact(document_id: uuid.UUID, sha256: str, fmt: str) -> _Facts:
        return facts.setdefault(document_id, _Facts(sha256, fmt))

    period_rows = uow.execute(
        select(
            statement_period.c.document_id,
            statement_period.c.account_id,
            statement_period.c.currency,
            statement_period.c.period_start,
            statement_period.c.period_end,
            statement_period.c.control_kind,
            statement_period.c.tied_out,
            document.c.sha256,
            document.c.format,
        )
        .join(document, document.c.id == statement_period.c.document_id)
        .where(document.c.status == "parsed")
    )
    for row in period_rows:
        each = fact(row.document_id, row.sha256, row.format)
        each.controls.setdefault(row.account_id, set()).add(
            (row.control_kind, row.tied_out)
        )
        each.periods.setdefault(row.account_id, set()).add(
            (row.currency, row.period_start, row.period_end)
        )
        if row.control_kind == "summary":
            each.summary = True

    line_rows = uow.execute(
        select(
            document_line.c.document_id,
            document_line.c.account_id,
            document.c.sha256,
            document.c.format,
        )
        .distinct()
        .join(document, document.c.id == document_line.c.document_id)
        .where(
            document.c.status == "parsed",
            document_line.c.line_kind == "transaction",
            document_line.c.account_id.is_not(None),
        )
    )
    for row in line_rows:
        each = fact(row.document_id, row.sha256, row.format)
        each.controls.setdefault(row.account_id, set())

    by_account: dict[uuid.UUID, list[Standing]] = defaultdict(list)
    for document_id, each in facts.items():
        for account_id, kinds in each.controls.items():
            by_account[account_id].append(
                Standing(
                    document_id=document_id,
                    account_id=account_id,
                    control=_control(kinds),
                    format_rank=_format_rank(each.fmt, each.summary),
                    sha256=each.sha256,
                    periods=frozenset(each.periods.get(account_id, ())),
                )
            )
    return {account: ranked(found) for account, found in by_account.items()}


LineFingerprint = frozenset[tuple[tuple[object, ...], int]]


def _line_fingerprints(uow: UnitOfWork) -> dict[uuid.UUID, LineFingerprint]:
    """Each parsed document's transaction lines as a multiset of (account, posting
    date, transaction date, amount, currency, normalized description)."""
    found: dict[uuid.UUID, Counter[tuple[object, ...]]] = defaultdict(Counter)
    rows = uow.execute(
        select(
            document_line.c.document_id,
            document_line.c.account_id,
            document_line.c.posting_date,
            document_line.c.txn_date,
            document_line.c.amount,
            document_line.c.currency,
            document_line.c.description_raw,
        )
        .join(document, document.c.id == document_line.c.document_id)
        .where(
            document.c.status == "parsed",
            document_line.c.line_kind == "transaction",
        )
    )
    for row in rows:
        found[row.document_id][
            (
                row.account_id,
                row.posting_date,
                row.txn_date,
                row.amount,
                row.currency,
                normalize_description(row.description_raw or ""),
            )
        ] += 1
    return {
        document_id: frozenset(lines.items()) for document_id, lines in found.items()
    }


def supersede(uow: UnitOfWork) -> int:
    """Mark ``superseded`` every parsed document that another parsed document with a
    larger sha256 matches on every account: the same control, format and periods, and
    the same transaction lines. Returns how many were marked."""
    signatures: dict[uuid.UUID, set[tuple[uuid.UUID, int, int, frozenset[Period]]]] = (
        defaultdict(set)
    )
    shas: dict[uuid.UUID, str] = {}
    for found in standings(uow).values():
        for each in found:
            shas[each.document_id] = each.sha256
            signatures[each.document_id].add(
                (each.account_id, each.control, each.format_rank, each.periods)
            )
    fingerprints = _line_fingerprints(uow)
    groups: dict[
        tuple[
            frozenset[tuple[uuid.UUID, int, int, frozenset[Period]]], LineFingerprint
        ],
        list[str],
    ] = defaultdict(list)
    for document_id, signature in signatures.items():
        # A document with no stated period on some account shares nothing it can be
        # compared on, so it is never superseded.
        if all(periods for _, _, _, periods in signature):
            key = (frozenset(signature), fingerprints.get(document_id, frozenset()))
            groups[key].append(shas[document_id])
    losers = [sha for same in groups.values() for sha in sorted(same, reverse=True)[1:]]
    if losers:
        uow.execute(
            update(document)
            .where(document.c.sha256.in_(losers))
            .values(status="superseded")
        )
    return len(losers)
