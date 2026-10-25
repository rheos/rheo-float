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
lines. A ``pair`` applies in full or not at all: it is inert (recorded, applying
nothing, taking nothing, so its lines resolve as if it were absent) when any one of
its edges is one no stage would store:

- it mixes fiat and crypto lines (no edge kind joins them);
- one fiat line: a fiat line has no one-sided edge;
- two fiat lines of one sign (a hand pair needs a debit and a credit), or a fiat line
  stage 8 does not read (zero, excluded) or, for an edge stage 8 would not store (not
  of reversal shape: one account, one currency, exactly opposite amounts), a line
  stage 9 does not read (a party's line);
- one crypto line that is not a withdrawal to an own address (``crypto_edges`` stores
  a one-sided edge for no other);
- two crypto lines on one account, in two assets or of one direction, or a crypto line
  ``crypto_edges`` does not read (a swap leg, a fee line, a labelled, excluded or zero
  line).

The line filters are the stages' own (each stage's ``LINES``), so this check and the
stages agree on what can be stored.

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
from decimal import Decimal

from sqlalchemy import and_, select

from rheo_finance.context import UnitOfWork
from rheo_finance.decision import DecisionKind, ItemRole, reversed_clause
from rheo_finance.derive import crypto_edges, pairing, stage8, stage9
from rheo_finance.manual.own_addresses import normalize_address
from rheo_finance.parsers.base import CRYPTO_TRANSFER, FIAT
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
    amount: Decimal
    currency: str
    # Read by stage 8 and by stage 9 (each stage's ``LINES``).
    reversal_line: bool
    transfer_line: bool
    # Read by ``crypto_edges`` (its ``LINES``, no label).
    crypto_line: bool
    # A crypto withdrawal ``crypto_edges`` reads whose destination is an own address.
    one_sided: bool


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
    labelled = crypto_edges.labelled(uow)
    owners = crypto_edges.owners(uow)
    found: dict[uuid.UUID, _LineFacts] = {}
    for row in uow.execute(
        select(
            txn.c.id,
            txn.c.account_id,
            txn.c.movement_kind,
            txn.c.amount,
            txn.c.currency,
            txn.c.to_address,
            and_(*stage8.LINES).label("reversal_line"),
            and_(*stage9.LINES).label("transfer_line"),
            and_(*crypto_edges.LINES).label("crypto_line"),
        ).where(txn.c.id.in_(ids))
    ):
        crypto_line = bool(row.crypto_line) and row.id not in labelled
        found[row.id] = _LineFacts(
            account_id=row.account_id,
            fiat=row.movement_kind == FIAT,
            amount=row.amount,
            currency=row.currency,
            reversal_line=bool(row.reversal_line),
            transfer_line=bool(row.transfer_line),
            crypto_line=crypto_line,
            one_sided=crypto_line
            and row.movement_kind == CRYPTO_TRANSFER
            and row.amount < 0
            and row.to_address is not None
            and normalize_address(row.to_address) in owners,
        )
    return found


def _opposite(a: _LineFacts, b: _LineFacts) -> bool:
    return (a.amount > 0) != (b.amount > 0)


def _fiat_edge(a: _LineFacts, b: _LineFacts) -> bool:
    """Whether stage 8 or stage 9 stores the edge of a ``pair`` joining ``a`` and
    ``b``: stage 8 one of reversal shape, stage 9 any other of opposite signs."""
    if not (a.reversal_line and b.reversal_line and _opposite(a, b)):
        return False
    reversal = (
        a.account_id == b.account_id
        and a.currency == b.currency
        and a.amount == -b.amount
    )
    return reversal or (a.transfer_line and b.transfer_line)


def _crypto_edge(a: _LineFacts, b: _LineFacts) -> bool:
    """Whether ``crypto_edges`` stores the edge of a ``pair`` joining ``a`` and
    ``b``: one asset, opposite directions."""
    return (
        a.crypto_line and b.crypto_line and _opposite(a, b) and a.currency == b.currency
    )


def _inert(lines: tuple[uuid.UUID, ...], facts: dict[uuid.UUID, _LineFacts]) -> bool:
    """Whether a ``pair`` of ``lines`` is inert: some edge of it no stage would store
    (module docstring)."""
    target, *others = (facts[line] for line in lines)
    if any(each.fiat != target.fiat for each in others):
        return True
    if not others:
        return not target.one_sided
    if target.fiat:
        return not all(_fiat_edge(target, other) for other in others)
    accounts = [target.account_id, *(each.account_id for each in others)]
    return len(set(accounts)) != len(accounts) or not all(
        _crypto_edge(target, other) for other in others
    )


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
