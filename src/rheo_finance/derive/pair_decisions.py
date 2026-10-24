"""The owner's ``pair`` and ``unpair`` decisions as the pairing stages read them (FR-15,
FR-18; SPEC Derivation stage 11, Data Models ``decision`` and ``decision_item``).

``read`` runs once per derive, after stage 7 and before stage 8, and its ``Decided``
goes to stages 8, 9 (fiat and crypto) and 11, so the candidate graph is built around
the decisions rather than repaired after them.

**Lines.** A decision names lines by ``finance.txn:<uuid>`` references whose id is a
line key (``txn_source.line_key_id``), never a txn id: the target, then the
counterpart, then each ``leg`` item in ``seq`` order. A key resolves to the txn that
carries it as a primary or an additional source, so a decision holds through a
rebuild and through a change of a line's primary source (the old key stays on the txn
as an additional source). A decision with a reference that is not a line (a notice,
which the notice stage reads) or a key no current line carries (an orphan, which the
orphan stage finds) is left out here, and so is one naming a line twice.

**Edge sets.** A ``pair`` joins its target to each other line it names, one edge
each; with no other line it names the target's ``own_one_sided`` edge (``{target}``).
An ``unpair`` names the one edge joining exactly its lines.

**Which apply.** Unreversed decisions (``decision.reversed_clause``, a bulk run's
reversal included), newest first (``decided_at``, then id). Each edge set belongs to
the newest decision naming it, so a later ``unpair`` undoes an earlier ``pair`` and the
other way round. A ``pair`` applies only when none of its edge sets is a newer
decision's and none of its lines is taken by a newer ``pair``; then it takes its
lines. A ``pair`` is inert (recorded, applying nothing, taking nothing) when a line is
excluded (stage 7), when it mixes fiat and crypto lines (no edge kind joins them),
when two of its crypto lines are on one account, or when it names one fiat line.

**What the stages do with it** (``Decided.allows``): an edge joining exactly an
``unpair``'s lines is never stored; a line a ``pair`` took is an end of no edge except
the edges of its decision, which the stages build when no ordinary candidate exists
(stage 8 a reversal-shaped pair, stage 9 any other fiat pair, ``crypto_edges`` a
crypto pair).
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field

from sqlalchemy import select

from rheo_finance.context import UnitOfWork
from rheo_finance.decision import DecisionKind, ItemRole, reversed_clause
from rheo_finance.derive import pairing
from rheo_finance.parsers.base import FIAT
from rheo_finance.storage import refs
from rheo_finance.storage.schema import decision, decision_item, txn, txn_source

LINE_TYPE = "txn"
KINDS = (DecisionKind.PAIR, DecisionKind.UNPAIR)

Ends = frozenset[uuid.UUID]


@dataclass(frozen=True)
class PairDecision:
    """One ``pair`` that applies: its lines (the target first) and its edge sets."""

    id: uuid.UUID
    lines: tuple[uuid.UUID, ...]

    @property
    def edge_sets(self) -> tuple[Ends, ...]:
        target, *others = self.lines
        if not others:
            return (frozenset({target}),)
        return tuple(frozenset({target, other}) for other in others)


@dataclass(frozen=True)
class Decided:
    """The decisions a derive applies (module docstring)."""

    # Newest first.
    pairs: tuple[PairDecision, ...] = ()
    unpaired: frozenset[Ends] = frozenset()
    taken: frozenset[uuid.UUID] = frozenset()
    joined: frozenset[Ends] = field(init=False)

    def __post_init__(self) -> None:
        sets = frozenset(ends for each in self.pairs for ends in each.edge_sets)
        object.__setattr__(self, "joined", sets)

    def allows(self, ends: Iterable[uuid.UUID]) -> bool:
        """Whether an edge joining exactly ``ends`` may be stored."""
        found = frozenset(ends)
        if found in self.unpaired:
            return False
        return found in self.joined or not found & self.taken


@dataclass(frozen=True)
class _Row:
    id: uuid.UUID
    kind: DecisionKind
    lines: tuple[uuid.UUID, ...]


@dataclass(frozen=True)
class _LineFacts:
    account_id: uuid.UUID
    fiat: bool
    excluded: bool


def _line_key(ref: str) -> uuid.UUID | None:
    parsed = refs.parse(ref)
    if parsed.system != refs.FINANCE_SYSTEM or parsed.type != LINE_TYPE:
        return None
    return uuid.UUID(parsed.id)


def _rows(uow: UnitOfWork) -> list[_Row]:
    """The unreversed ``pair`` and ``unpair`` decisions whose lines all resolve, newest
    first."""
    rows = uow.execute(
        select(
            decision.c.id,
            decision.c.kind,
            decision.c.target_ref,
            decision.c.counterpart_ref,
        )
        .where(
            decision.c.kind.in_([kind.value for kind in KINDS]),
            ~reversed_clause(decision),
        )
        .order_by(decision.c.decided_at.desc(), decision.c.id.desc())
    ).all()
    if not rows:
        return []
    legs: dict[uuid.UUID, list[str]] = defaultdict(list)
    for leg in uow.execute(
        select(decision_item.c.decision_id, decision_item.c.target_ref)
        .where(
            decision_item.c.role == ItemRole.LEG.value,
            decision_item.c.decision_id.in_([row.id for row in rows]),
        )
        .order_by(decision_item.c.decision_id, decision_item.c.seq)
    ):
        legs[leg.decision_id].append(leg.target_ref)
    keys = {
        row.id: [
            _line_key(ref)
            for ref in (row.target_ref, row.counterpart_ref, *legs[row.id])
            if ref is not None
        ]
        for row in rows
    }
    wanted = {key for found in keys.values() for key in found if key is not None}
    resolved = {
        row.line_key_id: row.txn_id
        for row in uow.execute(
            select(txn_source.c.line_key_id, txn_source.c.txn_id)
            .distinct()
            .where(
                txn_source.c.line_key_id.in_(wanted),
                txn_source.c.role.in_(pairing.LINE_ROLES),
            )
        )
    }
    found: list[_Row] = []
    for row in rows:
        lines = [None if key is None else resolved.get(key) for key in keys[row.id]]
        ids = tuple(line for line in lines if line is not None)
        if ids and len(ids) == len(lines) and len(set(ids)) == len(ids):
            found.append(_Row(row.id, DecisionKind(row.kind), ids))
    return found


def _facts(uow: UnitOfWork, ids: set[uuid.UUID]) -> dict[uuid.UUID, _LineFacts]:
    if not ids:
        return {}
    return {
        row.id: _LineFacts(
            row.account_id, row.movement_kind == FIAT, row.exclusion is not None
        )
        for row in uow.execute(
            select(
                txn.c.id, txn.c.account_id, txn.c.movement_kind, txn.c.exclusion
            ).where(txn.c.id.in_(ids))
        )
    }


def _inert(lines: tuple[uuid.UUID, ...], facts: dict[uuid.UUID, _LineFacts]) -> bool:
    """Whether a ``pair`` of ``lines`` can join nothing (module docstring)."""
    found = [facts[line] for line in lines]
    if any(each.excluded for each in found):
        return True
    kinds = {each.fiat for each in found}
    if len(kinds) == 2:
        return True
    if kinds == {True}:
        return len(found) == 1
    accounts = [each.account_id for each in found]
    return len(set(accounts)) != len(accounts)


def read(uow: UnitOfWork) -> Decided:
    """The decisions this derive applies (module docstring)."""
    rows = _rows(uow)
    facts = _facts(uow, {line for row in rows for line in row.lines})
    claimed: set[Ends] = set()
    taken: set[uuid.UUID] = set()
    unpaired: set[Ends] = set()
    pairs: list[PairDecision] = []
    for row in rows:
        if row.kind is DecisionKind.UNPAIR:
            ends = frozenset(row.lines)
            if ends not in claimed:
                claimed.add(ends)
                unpaired.add(ends)
            continue
        candidate = PairDecision(row.id, row.lines)
        sets = set(candidate.edge_sets)
        if _inert(row.lines, facts) or sets & claimed or taken & set(row.lines):
            continue
        claimed |= sets
        taken |= set(row.lines)
        pairs.append(candidate)
    return Decided(tuple(pairs), frozenset(unpaired), frozenset(taken))
