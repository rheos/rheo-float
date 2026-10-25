"""What the pairing stages share (stage 8 reversals, stage 9 candidate edges, stage
11): which sources are a txn's own lines, the read of those lines with their wording,
the stored edges, and the split of a set of edges into the groups of lines they
connect.

A txn's own lines are its primary and additional sources (``LINE_ROLES``); an
evidence source, such as a receipt, is not a line of it. A manual cash line has no
document line, so its wording is the txn's own.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Final

from sqlalchemy import ColumnElement, Row, Select, and_, select

from rheo_finance.context import UnitOfWork
from rheo_finance.storage.schema import document_line, pair_edge, txn, txn_source

# The source roles whose line is the txn itself; an evidence source is not.
LINE_ROLES: Final = ("primary", "additional")


@dataclass(frozen=True)
class StoredEdge:
    """One ``pair_edge`` row as stage 11 reads it."""

    id: uuid.UUID
    kind: str
    # ``a_txn_id`` first, then ``b_txn_id`` when there is one.
    ends: tuple[uuid.UUID, ...]
    spread: Decimal | None = None
    printed_fee: uuid.UUID | None = None


def stored_edges(uow: UnitOfWork) -> list[StoredEdge]:
    rows = uow.execute(
        select(pair_edge.c.id, pair_edge.c.kind, pair_edge.c.a_txn_id,
               pair_edge.c.b_txn_id, pair_edge.c.spread_cad, pair_edge.c.fee_txn_id)
    )  # fmt: skip
    return [
        StoredEdge(
            row.id,
            row.kind,
            tuple(end for end in (row.a_txn_id, row.b_txn_id) if end),
            row.spread_cad,
            row.fee_txn_id,
        )
        for row in rows
    ]


def source_lines(*columns: ColumnElement[Any]) -> Select[Any]:
    """One row per txn and line source (one row with no source line for a txn that
    has none), carrying ``id``, ``document_line_id`` and the wording columns
    ``wording`` reads, then ``columns``; primary source first within a txn, so a
    fold over the rows sees them in a fixed order. Callers add their filters."""
    return (
        select(
            txn.c.id,
            txn.c.description_norm.label("txn_wording"),
            txn_source.c.document_line_id,
            document_line.c.description_norm.label("line_wording"),
            *columns,
        )
        .select_from(txn)
        .outerjoin(
            txn_source,
            and_(txn_source.c.txn_id == txn.c.id, txn_source.c.role.in_(LINE_ROLES)),
        )
        .outerjoin(document_line, document_line.c.id == txn_source.c.document_line_id)
        .order_by(txn.c.id, txn_source.c.role.desc())
    )


def wording(row: Row[Any]) -> str:
    """The wording of one ``source_lines`` row: its document line's, else the txn's."""
    text = row.txn_wording if row.document_line_id is None else row.line_wording
    return text or ""


def groups[E](
    edges: Iterable[E], ends: Callable[[E], tuple[uuid.UUID, uuid.UUID]]
) -> list[list[E]]:
    """``edges`` split into the groups of lines they connect (``ends`` gives an
    edge's two lines), each group in the order its edges were given."""
    parent: dict[uuid.UUID, uuid.UUID] = {}

    def root(node: uuid.UUID) -> uuid.UUID:
        while parent.setdefault(node, node) != node:
            parent[node] = parent[parent[node]]
            node = parent[node]
        return node

    edges = list(edges)
    for edge in edges:
        a, b = ends(edge)
        parent[root(a)] = root(b)
    grouped: dict[uuid.UUID, list[E]] = defaultdict(list)
    for edge in edges:
        grouped[root(ends(edge)[0])].append(edge)
    return list(grouped.values())
