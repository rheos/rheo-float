"""Derivation stage 9, fiat own transfers: the candidate edges (FR-15, Key decision 9,
edge cases 4 and 5).

Stage 9 stores every candidate ``own_transfer`` edge in ``pair_edge`` with ``applied``
false; stage 11 decides which apply. On the lines it reads it also sets
``txn.transfer_like`` and ``txn.hint_account_id``.

**Which lines.** Fiat txns with a nonzero amount, never a line stage 8 paired as a
reversal (``pair_kind = reversal``), an excluded line, or a party line. Reading a line
means reading its primary and additional source lines (never an evidence source, such
as a receipt); a manual cash line, with no document line, has the txn's own wording.

**Transfer-like** (the lines that start a pairing): a source line carries the parser's
``transfer_hint`` or ``conversion_hint``, or its wording matches an ``out``, ``in`` or
``conversion`` row of ``transfer_hints.csv`` (``reference.patterns.row_hints``, the
matching ``classify`` uses).

**The hint account.** A line's ``hint_account_slug`` names its counterpart in two
ways, told apart by the transfer kind (prompt 29): a built-in pattern's counterpart is
an issuer key of the archive's issuer map, which resolves to every account that lines
of that issuer's documents are on; a ``transfer_hints.csv`` row's (kind ``manual_out``
or ``manual_in``) is the owner's own account slug, which resolves to that account. A
parser's hint wins over a row on the same source line, as in ``classify``, and the
first source line (the primary first) that names a counterpart gives the txn's.
``hint_account_id`` is set when the hint resolves to exactly one account.

**Candidates.** An edge joins a transfer-like line (the start) to a line of the
opposite sign on another account, posted at most ``finance.transfers.window_days``
apart, when:

- the start's hint, when it resolved, is a preference (the search looks on that
  account first): the start's candidates are the lines on the hinted account or
  accounts when any qualifies there, else the lines on every own account. The other
  end's hint is no filter: it acts only when that line is itself a start, so a card
  whose payment wording names one bank still pairs with a payment from another;
- on a card account (kind ``credit_card``, whose parsers mark the payment credits with
  a payment hint) the card-side line is itself transfer-like, so a purchase refund of
  the same amount is never offered as the other leg of a chequing debit;
- the two legs net to zero within the tolerance: the larger of
  ``finance.transfers.fee_tolerance_abs`` (CAD) and ``fee_tolerance_pct`` percent of
  the larger leg's CAD value (stage 4). A pair outside it gets no edge.

**Unconverted legs.** Two legs in one currency compare natively: the net is the sum of
the amounts, and the CAD tolerance is carried into that currency at the larger leg's
own CAD value (exact for CAD). When the larger leg has no CAD value, the percent part
alone applies, since the absolute part is a CAD figure. Legs in two currencies compare
by their CAD values; when either is ``unconverted`` there is no edge, because nothing
can be measured.

**Equal card payments.** For each group of lines the edges connect: when every debit
is a non-card line, every credit a card-side line, all of one amount and currency,
the debits and the credits are the same number (two or more), every debit has every
credit as a candidate (the sets are mutual), and no two debits and no two credits share
a posting date, the edges thin to the date order: the k-th debit by posting date to the
k-th credit. Each kept edge was a candidate, so it lies inside the window. Any other
group keeps every edge. The result depends on the two date-sorted lists only, never on
the order lines were read.
"""

from __future__ import annotations

import bisect
import dataclasses
import datetime as dt
import uuid
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Final

from sqlalchemy import and_, bindparam, insert, select, update

from rheo_finance.context import UnitOfWork
from rheo_finance.manual.transfer_hints import TransferHintRow
from rheo_finance.reference.patterns import row_hints
from rheo_finance.storage.ids import derived_id
from rheo_finance.storage.schema import (
    account,
    document,
    document_line,
    pair_edge,
    txn,
    txn_source,
)

WINDOW_KEY: Final = "finance.transfers.window_days"
ABS_KEY: Final = "finance.transfers.fee_tolerance_abs"
PCT_KEY: Final = "finance.transfers.fee_tolerance_pct"
KIND: Final = "own_transfer"
# The source roles whose line is the txn itself; an evidence source is not.
LINE_ROLES: Final = ("primary", "additional")
CARD: Final = "credit_card"
# The transfer kind of a ``transfer_hints.csv`` row: its counterpart is an account slug.
MANUAL_PREFIX: Final = "manual_"
# The kind recorded for a parser's hint, whose counterpart is an issuer key.
PATTERN: Final = "pattern"
HUNDRED: Final = Decimal(100)


@dataclass(frozen=True)
class Tolerance:
    window_days: int
    abs_cad: Decimal
    pct: Decimal


@dataclass(frozen=True)
class _Line:
    id: uuid.UUID
    account_id: uuid.UUID
    card: bool
    currency: str
    amount: Decimal
    cad: Decimal | None
    day: dt.date
    transfer_like: bool
    # (transfer kind, counterpart) of the first source line naming one.
    hint: tuple[str, str] | None


@dataclass
class _Reading:
    """One txn's source lines, folded as they are read."""

    line: _Line
    transfer_like: bool = False
    hint: tuple[str, str] | None = None


def _lines(uow: UnitOfWork, hint_rows: Sequence[TransferHintRow]) -> list[_Line]:
    rows = uow.execute(
        select(
            txn.c.id,
            txn.c.account_id,
            account.c.kind,
            txn.c.currency,
            txn.c.amount,
            txn.c.cad_amount,
            txn.c.posting_date,
            txn.c.description_norm.label("txn_wording"),
            txn_source.c.document_line_id,
            document_line.c.transfer_hint,
            document_line.c.conversion_hint,
            document_line.c.hint_account_slug,
            document_line.c.description_norm.label("line_wording"),
        )
        .select_from(txn)
        .join(account, account.c.id == txn.c.account_id)
        .outerjoin(
            txn_source,
            and_(txn_source.c.txn_id == txn.c.id, txn_source.c.role.in_(LINE_ROLES)),
        )
        .outerjoin(document_line, document_line.c.id == txn_source.c.document_line_id)
        .where(
            txn.c.movement_kind == "fiat",
            txn.c.amount != 0,
            txn.c.exclusion.is_(None),
            txn.c.party_id.is_(None),
            txn.c.pair_kind.is_distinct_from("reversal"),
        )
        # The primary source first, so its counterpart wins.
        .order_by(txn.c.id, txn_source.c.role.desc())
    )
    found: dict[uuid.UUID, _Reading] = {}
    for row in rows:
        reading = found.get(row.id)
        if reading is None:
            reading = found[row.id] = _Reading(
                _Line(
                    id=row.id,
                    account_id=row.account_id,
                    card=row.kind == CARD,
                    currency=row.currency,
                    amount=row.amount,
                    cad=row.cad_amount,
                    day=row.posting_date,
                    transfer_like=False,
                    hint=None,
                )
            )
        if row.document_line_id is None:
            wording, parser_transfer, parser_conversion, parser_slug = (
                row.txn_wording, False, False, None,
            )  # fmt: skip
        else:
            wording = row.line_wording
            parser_transfer = bool(row.transfer_hint)
            parser_conversion = bool(row.conversion_hint)
            parser_slug = row.hint_account_slug
        rows_say = row_hints(wording or "", hint_rows)
        reading.transfer_like = reading.transfer_like or any(
            (
                parser_transfer,
                parser_conversion,
                rows_say.transfer_hint is not None,
                rows_say.conversion_hint,
            )
        )
        if reading.hint is None:
            if parser_transfer:
                if parser_slug is not None:
                    reading.hint = (PATTERN, parser_slug)
            elif rows_say.transfer_hint is not None and rows_say.hint_account_slug:
                reading.hint = (rows_say.transfer_hint, rows_say.hint_account_slug)
    return [
        dataclasses.replace(each.line, transfer_like=each.transfer_like, hint=each.hint)
        for each in found.values()
    ]


def _resolver(uow: UnitOfWork) -> Mapping[tuple[str, str], frozenset[uuid.UUID]]:
    """Every (kind, counterpart) a hint can name, to its accounts: an owner's account
    slug for a ``manual_`` kind, an issuer key for a parser's pattern."""
    resolved: dict[tuple[str, str], set[uuid.UUID]] = defaultdict(set)
    for slug, account_id in uow.execute(select(account.c.slug, account.c.id)):
        resolved[(MANUAL_PREFIX, slug)].add(account_id)
    issued = (
        select(document.c.issuer, document_line.c.account_id)
        .distinct()
        .join(document, document.c.id == document_line.c.document_id)
        .where(document_line.c.account_id.is_not(None))
    )
    for issuer, account_id in uow.execute(issued):
        resolved[(PATTERN, issuer)].add(account_id)
    return {key: frozenset(ids) for key, ids in resolved.items()}


def _accounts(
    hint: tuple[str, str] | None,
    resolved: Mapping[tuple[str, str], frozenset[uuid.UUID]],
) -> frozenset[uuid.UUID]:
    """The accounts a hint names; empty when there is no hint or it names none."""
    if hint is None:
        return frozenset()
    kind, counterpart = hint
    key = MANUAL_PREFIX if kind.startswith(MANUAL_PREFIX) else PATTERN
    return resolved.get((key, counterpart), frozenset())


def within(a: _Line, b: _Line, tolerance: Tolerance) -> bool:
    """Whether the two legs net to zero within the tolerance (module docstring)."""
    pct = tolerance.pct / HUNDRED
    if a.currency == b.currency:
        net = a.amount + b.amount
        larger = a if abs(a.amount) >= abs(b.amount) else b
        if larger.cad is None or larger.cad == 0:
            allowed = abs(larger.amount) * pct
        else:
            cad = abs(larger.cad)
            allowed = max(tolerance.abs_cad, cad * pct) * abs(larger.amount) / cad
        return abs(net) <= allowed
    if a.cad is None or b.cad is None:
        return False
    larger_cad = max(abs(a.cad), abs(b.cad))
    return abs(a.cad + b.cad) <= max(tolerance.abs_cad, larger_cad * pct)


@dataclass(frozen=True)
class _Edge:
    debit: _Line
    credit: _Line


def _candidate_edges(
    lines: list[_Line],
    named: Mapping[uuid.UUID, frozenset[uuid.UUID]],
    tolerance: Tolerance,
) -> set[_Edge]:
    by_day = sorted(lines, key=lambda line: (line.day, line.id))
    days = [line.day for line in by_day]
    window = dt.timedelta(days=tolerance.window_days)

    def fits(start: _Line, other: _Line) -> bool:
        return (
            other.account_id != start.account_id
            and (other.amount > 0) != (start.amount > 0)
            and (not other.card or other.transfer_like)
            and within(start, other, tolerance)
        )

    edges: set[_Edge] = set()
    for start in lines:
        if not start.transfer_like:
            continue
        first = bisect.bisect_left(days, start.day - window)
        last = bisect.bisect_right(days, start.day + window)
        found = [other for other in by_day[first:last] if fits(start, other)]
        hinted = [other for other in found if other.account_id in named[start.id]]
        for other in hinted or found:
            debit, credit = (start, other) if start.amount < 0 else (other, start)
            edges.add(_Edge(debit, credit))
    return edges


def _groups(edges: Iterable[_Edge]) -> list[list[_Edge]]:
    """The edges split into the groups of lines they connect."""
    parent: dict[uuid.UUID, uuid.UUID] = {}

    def root(node: uuid.UUID) -> uuid.UUID:
        while parent.setdefault(node, node) != node:
            parent[node] = parent[parent[node]]
            node = parent[node]
        return node

    edges = list(edges)
    for edge in edges:
        parent[root(edge.debit.id)] = root(edge.credit.id)
    grouped: dict[uuid.UUID, list[_Edge]] = defaultdict(list)
    for edge in edges:
        grouped[root(edge.debit.id)].append(edge)
    return list(grouped.values())


def _thinned(group: list[_Edge]) -> list[_Edge]:
    """The equal-card-payments tie-break (module docstring), or the group as it is."""
    debits = sorted({e.debit for e in group}, key=lambda line: line.day)
    credits = sorted({e.credit for e in group}, key=lambda line: line.day)
    amounts = {(line.currency, abs(line.amount)) for line in debits + credits}
    if (
        len(debits) < 2
        or len(debits) != len(credits)
        or len(group) != len(debits) * len(credits)
        or len(amounts) != 1
        or any(line.card for line in debits)
        or not all(line.card for line in credits)
        or len({line.day for line in debits}) != len(debits)
        or len({line.day for line in credits}) != len(credits)
    ):
        return group
    return [_Edge(d, c) for d, c in zip(debits, credits, strict=True)]


def pair_transfers(
    uow: UnitOfWork,
    *,
    tolerance: Tolerance,
    hint_rows: Sequence[TransferHintRow],
) -> None:
    """Store every fiat ``own_transfer`` candidate edge and set ``transfer_like`` and
    ``hint_account_id`` (module docstring)."""
    lines = _lines(uow, hint_rows)
    resolved = _resolver(uow)
    named = {line.id: _accounts(line.hint, resolved) for line in lines}
    edges = [
        edge
        for group in _groups(_candidate_edges(lines, named, tolerance))
        for edge in _thinned(group)
    ]
    edges.sort(key=lambda edge: (edge.debit.day, edge.debit.id, edge.credit.id))
    if edges:
        uow.execute(
            insert(pair_edge),
            [
                {
                    "id": derived_id(
                        edge.debit.day,
                        ("pair_edge", KIND, edge.debit.id, edge.credit.id),
                    ),
                    "kind": KIND,
                    "a_txn_id": edge.debit.id,
                    "b_txn_id": edge.credit.id,
                    "applied": False,
                }
                for edge in edges
            ],
        )
    marked = [
        {
            "b_id": line.id,
            "b_hint": next(iter(named[line.id])) if len(named[line.id]) == 1 else None,
        }
        for line in lines
        if line.transfer_like
    ]
    if marked:
        uow.execute(
            update(txn)
            .where(txn.c.id == bindparam("b_id"))
            .values(transfer_like=True, hint_account_id=bindparam("b_hint")),
            marked,
        )
