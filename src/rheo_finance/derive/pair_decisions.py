"""The owner's ``pair`` and ``unpair`` decisions as stage 11 reads them (FR-15, FR-18;
SPEC Derivation stage 11, Data Models ``decision`` and ``decision_item``).

A decision names lines by ``finance.txn:<uuid>`` references whose id is a line key
(``txn_source.line_key_id``), never a txn id: the target, then the counterpart, then
each ``leg`` item in its ``seq`` order. A key resolves to the txn that carries it as
a primary or an additional source, so a decision keeps working after its lines are
re-derived, and a line whose primary source changed is still found through the old
key, which stays on it as an additional source.

``line_decisions`` gives the decisions that apply, newest first (``decided_at``, then
id):

- only unreversed ``pair`` and ``unpair`` decisions, a bulk run's reversal included
  (``decision.reversed_clause``);
- a decision any of whose references is not a line reference (a notice, which the
  notice stage reads) or names a key no current line carries (an orphan, which the
  orphan stage finds) is left out, and so is one that names one line twice;
- of the decisions naming the same set of lines, only the newest, so a later
  ``unpair`` undoes an earlier ``pair`` of those lines and a later ``pair`` undoes an
  earlier ``unpair``.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from dataclasses import dataclass

from sqlalchemy import select

from rheo_finance.context import UnitOfWork
from rheo_finance.decision import DecisionKind, ItemRole, reversed_clause
from rheo_finance.derive import pairing
from rheo_finance.storage import refs
from rheo_finance.storage.schema import decision, decision_item, txn_source

LINE_TYPE = "txn"
KINDS = (DecisionKind.PAIR, DecisionKind.UNPAIR)


@dataclass(frozen=True)
class LineDecision:
    """One ``pair`` or ``unpair`` decision with its lines resolved to txn ids, the
    target first."""

    id: uuid.UUID
    kind: DecisionKind
    lines: tuple[uuid.UUID, ...]


def _line_key(ref: str) -> uuid.UUID | None:
    parsed = refs.parse(ref)
    if parsed.system != refs.FINANCE_SYSTEM or parsed.type != LINE_TYPE:
        return None
    return uuid.UUID(parsed.id)


def line_decisions(uow: UnitOfWork) -> list[LineDecision]:
    """The ``pair`` and ``unpair`` decisions that apply, newest first (module
    docstring)."""
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
    named = {
        row.id: [
            ref
            for ref in (row.target_ref, row.counterpart_ref, *legs[row.id])
            if ref is not None
        ]
        for row in rows
    }
    keys = {key: [_line_key(ref) for ref in found] for key, found in named.items()}
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
    found: list[LineDecision] = []
    seen: set[frozenset[uuid.UUID]] = set()
    for row in rows:
        lines = [None if key is None else resolved.get(key) for key in keys[row.id]]
        if not lines or any(line is None for line in lines):
            continue
        ids = tuple(line for line in lines if line is not None)
        if len(set(ids)) != len(ids) or frozenset(ids) in seen:
            continue
        seen.add(frozenset(ids))
        found.append(LineDecision(row.id, DecisionKind(row.kind), ids))
    return found
