"""The one-to-one overlap matcher: which document lines become txns, which join a txn
as an additional source, and which are source discrepancies (SPEC "Derivation" stage
3, FR-4, Technical Risk 1). Pure: it reads lines and standings and returns decisions;
``stage3`` reads the parse cache and writes the tables.

Per account, documents are taken in precedence order (``precedence``):

1. **Statements own their periods.** Every line of a tied-out statement dated inside
   its own period and outside the periods of the statements ranked before it becomes
   a txn, before anything is matched. Two statements of one account never match each
   other's lines across a period boundary, so a purchase repeated on either side of a
   boundary is two purchases.
2. **Every other line is matched**, document by document in precedence order, one to
   one against the txns other documents committed on the account: same currency and
   amount, and a posting or transaction date of the line within ``overlap_days`` of a
   posting or transaction date of the txn. A line takes, in this order of preference,
   the txn its own natural key already names, then a txn on an exact date with the
   same normalized description, then one on an exact date, then one inside the
   tolerance with the same description, then any inside the tolerance; among equals,
   the earliest txn. Each txn takes at most one line of a document. A matched line is
   an additional source of its txn and adds no txn.
3. **An unmatched line** becomes a txn when its posting date is outside every
   tied-out statement period of the account in its currency, and is a
   ``unmatched_in_statement_window`` discrepancy when it is inside one: a tied-out
   statement owns its period.

Statement periods are per currency (a statement can summarize each currency on its
own), so a USD period owns no CAD line. Within one document, lines are matched in
posting date order, then line order, whatever order the file lists them in: an export
printed newest first must not hand an older purchase's txn to a newer line.

A line key names one txn. A line whose own key already names a txn that another line
of the same document has taken (the document repeats a purchase the other sources
saw once) cannot join a second txn under that key or start one: it is a
``line_key_taken`` discrepancy, inside a statement period or outside every one. SPEC
would make it a txn outside every period; it has no key of its own to take (ADR-0010).

A txn's basis is its first document's standing (statement, verified or unverified
export). A txn from an export is provisional when the account has a tied-out
statement in its currency and the txn is dated after the last one ends. A later
statement covering it puts its own line first: the export line then matches it as an
additional source and keeps its key, so a reference taken on the provisional txn
still resolves.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Final, Literal

from rheo_finance.derive.precedence import STATEMENT, Basis, Standing

DiscrepancyKind = Literal["unmatched_in_statement_window", "line_key_taken"]
DISCREPANCY_KINDS: Final[tuple[DiscrepancyKind, ...]] = (
    "unmatched_in_statement_window",
    "line_key_taken",
)


@dataclass(frozen=True)
class Line:
    """One transaction line of a parsed document, with its natural key."""

    id: uuid.UUID
    document_id: uuid.UUID
    account_id: uuid.UUID
    posting_date: dt.date
    txn_date: dt.date | None
    amount: Decimal
    currency: str
    description_norm: str
    key: uuid.UUID

    @property
    def dates(self) -> tuple[dt.date, ...]:
        if self.txn_date is None or self.txn_date == self.posting_date:
            return (self.posting_date,)
        return (self.posting_date, self.txn_date)


@dataclass(frozen=True)
class Commit:
    """A line that becomes a txn, whose id is the line's key."""

    line: Line
    basis: Basis
    provisional: bool


@dataclass(frozen=True)
class Join:
    """A line that is an additional source of ``txn_id``."""

    line: Line
    txn_id: uuid.UUID


@dataclass(frozen=True)
class Discrepancy:
    line: Line
    kind: DiscrepancyKind


@dataclass
class Outcome:
    commits: list[Commit] = field(default_factory=list)
    joins: list[Join] = field(default_factory=list)
    discrepancies: list[Discrepancy] = field(default_factory=list)


@dataclass(frozen=True)
class _Txn:
    """A committed txn as a match candidate. ``seq`` is commit order, the last tie
    break, which follows precedence and line order and nothing else."""

    id: uuid.UUID
    document_id: uuid.UUID
    dates: tuple[dt.date, ...]
    posting_date: dt.date
    description_norm: str
    seq: int


Window = tuple[dt.date, dt.date]


def _inside(day: dt.date, windows: Iterable[Window]) -> bool:
    return any(start <= day <= end for start, end in windows)


def _gap(line: Line, txn: _Txn) -> int:
    return min(abs((a - b).days) for a in line.dates for b in txn.dates)


Prefer = Callable[[Line, _Txn, int], bool]


def _preferences(overlap_days: int) -> tuple[Prefer, ...]:
    return (
        lambda line, txn, gap: (
            gap == 0 and line.description_norm == txn.description_norm
        ),
        lambda line, txn, gap: gap == 0,
        lambda line, txn, gap: (
            gap <= overlap_days and line.description_norm == txn.description_norm
        ),
        lambda line, txn, gap: gap <= overlap_days,
    )


class _Account:
    """The matcher's state for one account."""

    def __init__(
        self,
        standings: Sequence[Standing],
        lines: Mapping[uuid.UUID, Sequence[Line]],
        overlap_days: int,
        key_owner: dict[uuid.UUID, uuid.UUID],
        outcome: Outcome,
    ) -> None:
        self.standings = standings
        self.lines = lines
        self.preferences = _preferences(overlap_days)
        self.key_owner = key_owner
        self.outcome = outcome
        self.windows: dict[str, list[Window]] = defaultdict(list)
        for each in standings:
            for currency, _, _ in each.periods:
                for window in each.windows(currency):
                    if window not in self.windows[currency]:
                        self.windows[currency].append(window)
        self.txns: dict[uuid.UUID, _Txn] = {}
        self.by_amount: dict[tuple[str, Decimal], list[_Txn]] = defaultdict(list)

    def commit(self, line: Line, standing: Standing) -> None:
        last_end = max(
            (end for _, end in self.windows.get(line.currency, ())), default=None
        )
        provisional = (
            standing.control != STATEMENT
            and last_end is not None
            and line.posting_date > last_end
        )
        self.outcome.commits.append(Commit(line, standing.basis, provisional))
        txn = _Txn(
            line.key,
            line.document_id,
            line.dates,
            line.posting_date,
            line.description_norm,
            len(self.txns),
        )
        self.txns[txn.id] = txn
        self.by_amount[line.currency, line.amount].append(txn)
        self.key_owner[line.key] = txn.id

    def run(self) -> None:
        pending: dict[uuid.UUID, list[Line]] = {}
        earlier: dict[str, list[Window]] = defaultdict(list)
        for standing in self.standings:
            rest = pending[standing.document_id] = []
            for line in self.lines.get(standing.document_id, ()):
                day, currency = line.posting_date, line.currency
                if _inside(day, standing.windows(currency)) and not _inside(
                    day, earlier[currency]
                ):
                    self.commit(line, standing)
                else:
                    rest.append(line)
            for currency, _, _ in standing.periods:
                earlier[currency].extend(standing.windows(currency))
        for standing in self.standings:
            self.match(pending[standing.document_id], standing)

    def match(self, in_file_order: Sequence[Line], standing: Standing) -> None:
        # Stable: lines of one date keep their order in the document.
        lines = sorted(in_file_order, key=lambda line: line.posting_date)
        taken: set[uuid.UUID] = set()
        found: dict[uuid.UUID, uuid.UUID] = {}
        blocked: set[uuid.UUID] = set()
        for line in lines:
            owner = self.key_owner.get(line.key)
            if owner is None:
                continue
            if owner in taken or owner not in self.txns:
                blocked.add(line.id)
            else:
                taken.add(owner)
                found[line.id] = owner
        for prefer in self.preferences:
            for line in lines:
                if line.id in found or line.id in blocked:
                    continue
                best: _Txn | None = None
                for txn in self.by_amount.get((line.currency, line.amount), ()):
                    if txn.document_id == line.document_id or txn.id in taken:
                        continue
                    if not prefer(line, txn, _gap(line, txn)):
                        continue
                    if best is None or (txn.posting_date, txn.seq) < (
                        best.posting_date,
                        best.seq,
                    ):
                        best = txn
                if best is not None:
                    taken.add(best.id)
                    found[line.id] = best.id
        for line in lines:
            if line.id in found:
                self.outcome.joins.append(Join(line, found[line.id]))
                self.key_owner[line.key] = found[line.id]
            elif line.id in blocked:
                self.outcome.discrepancies.append(Discrepancy(line, "line_key_taken"))
            elif _inside(line.posting_date, self.windows.get(line.currency, ())):
                self.outcome.discrepancies.append(
                    Discrepancy(line, "unmatched_in_statement_window")
                )
            else:
                self.commit(line, standing)


def match_lines(
    standings: Mapping[uuid.UUID, Sequence[Standing]],
    lines: Mapping[tuple[uuid.UUID, uuid.UUID], Sequence[Line]],
    overlap_days: int,
) -> Outcome:
    """Decide every line. ``standings`` maps an account to its documents in
    precedence order; ``lines`` maps (account, document) to that document's lines on
    the account in line order."""
    outcome = Outcome()
    key_owner: dict[uuid.UUID, uuid.UUID] = {}
    for account_id in sorted(standings):
        by_document = {
            each.document_id: lines.get((account_id, each.document_id), ())
            for each in standings[account_id]
        }
        _Account(
            standings[account_id], by_document, overlap_days, key_owner, outcome
        ).run()
    return outcome
