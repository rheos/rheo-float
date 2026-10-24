"""What the pairing stages share (stage 8 reversals, stage 9 candidate edges): which
sources are a txn's own lines, the read of those lines with their wording, and the
split of a set of edges into the groups of lines they connect.

A txn's own lines are its primary and additional sources (``LINE_ROLES``); an
evidence source, such as a receipt, is not a line of it. A manual cash line has no
document line, so its wording is the txn's own.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from collections.abc import Callable, Iterable
from typing import Any, Final

from sqlalchemy import ColumnElement, Row, Select, and_, select

from rheo_finance.storage.schema import document_line, txn, txn_source

# The source roles whose line is the txn itself; an evidence source is not.
LINE_ROLES: Final = ("primary", "additional")


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
