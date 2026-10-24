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
on every account they cover, for exactly the same periods, with the same parse output
(every line of every kind and every period's figures), the one with the larger sha256
stays ``parsed`` and the others become ``superseded``. The content is compared too:
two exports can state the same period and still differ inside it, and two bills of
one month can differ in the amount due; superseding one would drop what it says
without a trace.
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
from rheo_finance.parsers.base import CRYPTO_LINE_KINDS
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
# The line kinds that become txns by the line rules of stage 3, so give a document a
# standing on their account even where it states no period there.
LINE_KINDS: Final = ("transaction", *CRYPTO_LINE_KINDS)


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

    def windows(self, currency: str) -> tuple[tuple[dt.date, dt.date], ...]:
        """The periods this document owns on the account in ``currency``: its tied-out
        statement periods in that currency, none for an export. A statement of one
        currency owns nothing of another."""
        if self.control != STATEMENT:
            return ()
        return tuple(
            sorted(
                {(start, end) for cur, start, end in self.periods if cur == currency}
            )
        )

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
    """Every parsed document's standing on each account it has periods or lines of a
    ``LINE_KINDS`` kind on, by account, in precedence order."""
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
            document_line.c.line_kind.in_(LINE_KINDS),
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


ParseFingerprint = frozenset[tuple[tuple[object, ...], int]]

# Columns that identify where a row sits, or that stage 3 fills, rather than what the
# document says: two copies of one document may differ in them.
_LINE_PLACE: Final = frozenset(
    {"id", "document_id", "line_no", "page", "description_norm", "line_key_id"}
)


def _parse_fingerprints(uow: UnitOfWork) -> dict[uuid.UUID, ParseFingerprint]:
    """Each parsed document's whole parse output as a multiset: every period row (its
    control figures, due date and tie-out included) and every line of every kind,
    with the raw description compared normalized."""
    found: dict[uuid.UUID, Counter[tuple[object, ...]]] = defaultdict(Counter)
    parsed = select(document.c.id).where(document.c.status == "parsed")
    period_columns = [c for c in statement_period.c if c.name != "document_id"]
    for row in uow.execute(
        select(statement_period.c.document_id, *period_columns).where(
            statement_period.c.document_id.in_(parsed)
        )
    ):
        found[row.document_id][("period", *row[1:])] += 1
    line_columns = [c for c in document_line.c if c.name not in _LINE_PLACE]
    for row in uow.execute(
        select(document_line.c.document_id, *line_columns).where(
            document_line.c.document_id.in_(parsed)
        )
    ):
        values = dict(row._mapping)
        values["description_raw"] = normalize_description(
            values["description_raw"] or ""
        )
        del values["document_id"]
        found[row.document_id][("line", *values.values())] += 1
    return {document_id: frozenset(rows.items()) for document_id, rows in found.items()}


def supersede(uow: UnitOfWork) -> int:
    """Mark ``superseded`` every parsed document that another parsed document with a
    larger sha256 matches on every account: the same control, format and periods, and
    the same parse output (``_parse_fingerprints``). Returns how many were marked."""
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
    fingerprints = _parse_fingerprints(uow)
    groups: dict[
        tuple[
            frozenset[tuple[uuid.UUID, int, int, frozenset[Period]]], ParseFingerprint
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
