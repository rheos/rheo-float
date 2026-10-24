"""Derivation stage 8: reversals, in both directions (FR-15(a), Key decision 9, edge
cases 33, 34, 63 and 81).

**A reversal line** is a txn one of whose lines (its primary or an additional source,
never an evidence source such as a receipt) its parser marked ``reversal_hint`` (the
issuer's wording in ``reference/patterns.json``: "debit reversal", "customer cancel",
"credit reversal", "return", "recall", "nsf" and the like), or carries wording a
``reversal`` row of ``transfer_hints.csv`` matches. Its sign is its direction: a
reversal credit undoes a debit (the owner's payment cancelled), a reversal debit undoes
a credit (a received payment clawed back). A line with reversal wording is never
itself the original of another reversal, and a line of the same amount without such
wording is never a reversal: the owner pairs it by decision.

**Its candidates** are the other lines on the same account, in the same currency, of
exactly the opposite amount, posted at most ``finance.reversals.window_days`` before
it. An original posted on the reversal's own day is a candidate only when there is no
earlier one, because a statement's order within a day is not a direction. Matching
reads the wording flags, the account, the currency, the amount and the posting date
only: the bank prints no payee, or not the original one, on a reversal.

**Outcomes**, for each group of lines the candidate edges connect:

- one edge, to an earlier original: the edge is applied, and both lines are
  ``pair_kind = reversal``, ``transfer_state = paired`` (shown as ``reversed``). A
  paired line adds zero to spending and income and is never payment evidence,
  allocation material, or a trade, loan or rent candidate; a confirmed ``split`` on a
  reversed credit stays a decision and makes no parts;
- one edge, to a same-day original: the reversal is ``ambiguous``, its confirm item
  showing that one candidate; the original stays an ordinary line, nothing netted;
- two or more edges (two candidates for one reversal, or two reversals for one
  original): every line of the group is ``ambiguous``, nothing netted;
- a reversal with no candidate is ``unresolved``.

Every candidate edge is stored in ``pair_edge`` (kind ``reversal``, ``a_txn_id`` the
reversal, ``b_txn_id`` the original), applied or not. A re-sent payment is an ordinary
line and counts once.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Final

from sqlalchemy import and_, bindparam, insert, select, update

from rheo_finance.context import UnitOfWork
from rheo_finance.manual.transfer_hints import TransferHintRow
from rheo_finance.reference.patterns import reversal_row_matches
from rheo_finance.storage.ids import derived_id
from rheo_finance.storage.schema import document_line, pair_edge, txn, txn_source

WINDOW_KEY: Final = "finance.reversals.window_days"
KIND: Final = "reversal"
# The source roles whose line is the txn itself; an evidence source (a receipt) is not.
LINE_ROLES: Final = ("primary", "additional")


@dataclass(frozen=True)
class _Line:
    id: uuid.UUID
    account_id: uuid.UUID
    currency: str
    amount: Decimal
    day: dt.date
    reversal: bool


@dataclass(frozen=True)
class _Edge:
    reversal: _Line
    original: _Line

    @property
    def same_day(self) -> bool:
        return self.original.day == self.reversal.day


def _lines(uow: UnitOfWork, hint_rows: Sequence[TransferHintRow]) -> list[_Line]:
    """Every nonzero txn, a reversal when any of its line sources (``LINE_ROLES``)
    carries the parser's ``reversal_hint`` or wording a ``reversal`` row matches. A
    source with no document line (a manual cash line) has the txn's own wording."""
    rows = uow.execute(
        select(
            txn.c.id,
            txn.c.account_id,
            txn.c.currency,
            txn.c.amount,
            txn.c.posting_date,
            txn.c.description_norm.label("txn_wording"),
            txn_source.c.document_line_id,
            document_line.c.reversal_hint,
            document_line.c.description_norm.label("line_wording"),
        )
        .select_from(txn)
        .outerjoin(
            txn_source,
            and_(txn_source.c.txn_id == txn.c.id, txn_source.c.role.in_(LINE_ROLES)),
        )
        .outerjoin(document_line, document_line.c.id == txn_source.c.document_line_id)
        .where(txn.c.amount != 0)
    )
    found: dict[uuid.UUID, _Line] = {}
    for row in rows:
        if row.document_line_id is None:
            hinted, wording = False, row.txn_wording
        else:
            hinted, wording = bool(row.reversal_hint), row.line_wording
        reversal = hinted or reversal_row_matches(wording or "", hint_rows)
        before = found.get(row.id)
        found[row.id] = _Line(
            id=row.id,
            account_id=row.account_id,
            currency=row.currency,
            amount=row.amount,
            day=row.posting_date,
            reversal=reversal or (before is not None and before.reversal),
        )
    return list(found.values())


def _candidate_edges(lines: list[_Line], window_days: int) -> list[_Edge]:
    """Each reversal's edges: to every earlier original in the window, else to every
    original on its own day."""
    originals: dict[tuple[uuid.UUID, str, Decimal], list[_Line]] = defaultdict(list)
    for line in lines:
        if not line.reversal:
            originals[(line.account_id, line.currency, line.amount)].append(line)
    window = dt.timedelta(days=window_days)
    edges: list[_Edge] = []
    for line in lines:
        if not line.reversal:
            continue
        found = originals.get((line.account_id, line.currency, -line.amount), [])
        earlier = [o for o in found if line.day - window <= o.day < line.day]
        chosen = earlier or [o for o in found if o.day == line.day]
        edges += [_Edge(line, original) for original in chosen]
    return edges


def _groups(edges: list[_Edge]) -> list[list[_Edge]]:
    """The edges split into the groups of lines they connect."""
    parent: dict[uuid.UUID, uuid.UUID] = {}

    def root(node: uuid.UUID) -> uuid.UUID:
        while parent.setdefault(node, node) != node:
            parent[node] = parent[parent[node]]
            node = parent[node]
        return node

    for edge in edges:
        parent[root(edge.reversal.id)] = root(edge.original.id)
    grouped: dict[uuid.UUID, list[_Edge]] = defaultdict(list)
    for edge in edges:
        grouped[root(edge.reversal.id)].append(edge)
    return list(grouped.values())


def pair_reversals(
    uow: UnitOfWork, *, window_days: int, hint_rows: Sequence[TransferHintRow]
) -> None:
    """Store every reversal edge and set the reversal states (module docstring)."""
    lines = _lines(uow, hint_rows)
    edges = _candidate_edges(lines, window_days)
    states: dict[uuid.UUID, tuple[str, str | None]] = {
        line.id: ("unresolved", None) for line in lines if line.reversal
    }
    applied: set[_Edge] = set()
    for group in _groups(edges):
        if len(group) == 1 and not group[0].same_day:
            applied.add(group[0])
            states[group[0].reversal.id] = ("paired", KIND)
            states[group[0].original.id] = ("paired", KIND)
        elif len(group) == 1:
            states[group[0].reversal.id] = ("ambiguous", None)
        else:
            for edge in group:
                states[edge.reversal.id] = ("ambiguous", None)
                states[edge.original.id] = ("ambiguous", None)
    if edges:
        uow.execute(
            insert(pair_edge),
            [
                {
                    "id": derived_id(
                        edge.reversal.day,
                        ("pair_edge", KIND, edge.reversal.id, edge.original.id),
                    ),
                    "kind": KIND,
                    "a_txn_id": edge.reversal.id,
                    "b_txn_id": edge.original.id,
                    "applied": edge in applied,
                }
                for edge in edges
            ],
        )
    if states:
        uow.execute(
            update(txn)
            .where(txn.c.id == bindparam("b_id"))
            .values(transfer_state=bindparam("b_state"), pair_kind=bindparam("b_kind")),
            [
                {"b_id": line_id, "b_state": state, "b_kind": kind}
                for line_id, (state, kind) in states.items()
            ],
        )
