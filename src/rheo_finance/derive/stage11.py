"""Derivation stage 11, resolution (core part, fiat): which candidate edges apply, the
transfer states, and the derived fee lines (FR-15, Key decision 9, edge cases 2, 3, 5
and 70).

**Mutual uniqueness.** An edge applies only when it is the only edge on both of its
ends, counting the edges of every kind in ``pair_edge`` together, and its kind allows
it (``AUTO_KINDS``: ``own_transfer``, ``own_one_sided`` and ``conversion``; the
``trade`` and ``loan_*`` kinds are decided by the peer-money stages). The result is a
function of the stored edge set alone, so it does not depend on the order lines were
imported or read.

**States.** On every line that is an end of an edge stage 11 decides:

- an end of an applied edge is ``transfer_state = paired`` with the edge's kind as
  its ``pair_kind``: it adds zero to spending and zero to income;
- any other end is ``ambiguous``, nothing applied (a confirm item);
- a transfer-like line (stage 9) left with no state is ``unresolved``: it has no edge
  of a kind decided here (an original that only a same-day reversal names included).

So each transfer-like line ends in exactly one of {paired, ambiguous, unresolved}.

**Reversals are settled already.** Stage 8 decides its ``reversal`` edges and their
lines' states. Stage 11 counts those edges (a line with a reversal edge and another
edge has two, so neither applies) but never changes a reversal edge, and a line whose
only edges are reversal edges keeps the state stage 8 gave it. A line stage 8 paired
is never a stage 9 candidate, so its one edge is its reversal edge.

**Derived fee lines.** An applied fiat ``own_transfer`` whose legs do not net to zero
(inside the tolerance, or stage 9 gave no edge) books the net as one txn of origin
``derived_fee`` on the OUTBOUND account (the debit's), dated and recognized as the
debit, ``fee_type`` ``fee``, with no transfer state, so it counts once in spending
(a positive net counts against it). Two legs in one currency net natively: in CAD the
fee is native, in another currency it takes the debit's own conversion (its rate,
its state, unconverted when the debit is). Legs in two currencies net by their CAD
values, and the fee is that CAD figure. The edge's ``fee_txn_id`` names the fee line.
Its id is ``derived_id`` over the edge, and its one source is a ``manual_ref`` naming
the edge (``FEE_REF``), since no document line prints it. A fee a parser already
printed is its own line before this stage and is netted out of nothing here: a
transfer whose legs balance after it makes no derived fee.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections import Counter
from dataclasses import dataclass
from decimal import Decimal
from typing import Final

from sqlalchemy import bindparam, insert, select, update

from rheo_finance.context import UnitOfWork
from rheo_finance.derive.stage4 import cents
from rheo_finance.storage.ids import derived_id
from rheo_finance.storage.schema import pair_edge, txn, txn_source

# The edge kinds that apply on their own when mutually unique, to the ``pair_kind``
# their lines get.
AUTO_KINDS: Final = {
    "own_transfer": "own_transfer",
    "own_one_sided": "own_one_sided",
    "conversion": "conversion",
}
# Stage 8's kind: counted, never decided here.
SETTLED: Final = "reversal"
FEE_KIND: Final = "own_transfer"
FIAT: Final = "fiat"
CAD: Final = "CAD"
FEE_ORIGIN: Final = "derived_fee"
FEE_TYPE: Final = "fee"
FEE_WORDING: Final = "derived fee"
# A derived fee's source reference: the prefix, then the edge's id.
FEE_REF: Final = "derived_fee:"


@dataclass(frozen=True)
class _Edge:
    id: uuid.UUID
    kind: str
    ends: tuple[uuid.UUID, ...]


@dataclass(frozen=True)
class _Leg:
    id: uuid.UUID
    account_id: uuid.UUID
    day: dt.date
    recognized: dt.date | None
    amount: Decimal
    currency: str
    cad: Decimal | None
    cad_source: str | None
    rate: Decimal | None
    rate_date: dt.date | None
    conversion_state: str | None
    movement_kind: str


def _edges(uow: UnitOfWork) -> list[_Edge]:
    rows = uow.execute(
        select(pair_edge.c.id, pair_edge.c.kind, pair_edge.c.a_txn_id,
               pair_edge.c.b_txn_id)
    )  # fmt: skip
    return [
        _Edge(
            row.id, row.kind, tuple(end for end in (row.a_txn_id, row.b_txn_id) if end)
        )
        for row in rows
    ]


def _legs(uow: UnitOfWork, ids: set[uuid.UUID]) -> dict[uuid.UUID, _Leg]:
    if not ids:
        return {}
    rows = uow.execute(
        select(
            txn.c.id, txn.c.account_id, txn.c.posting_date, txn.c.recognition_date,
            txn.c.amount, txn.c.currency, txn.c.cad_amount, txn.c.cad_source,
            txn.c.rate, txn.c.rate_date, txn.c.conversion_state, txn.c.movement_kind,
        ).where(txn.c.id.in_(ids))
    )  # fmt: skip
    return {row.id: _Leg(*row) for row in rows}


def _fee(edge: _Edge, a: _Leg, b: _Leg) -> dict[str, object] | None:
    """The derived fee txn of one applied fiat ``own_transfer``, or None when its legs
    net to zero or cannot be measured (module docstring)."""
    debit, credit = (a, b) if a.amount < 0 else (b, a)
    native: dict[str, object] = {
        "cad_source": "native", "rate": None, "rate_date": None,
        "conversion_state": "native",
    }  # fmt: skip
    if debit.currency == credit.currency:
        net = debit.amount + credit.amount
        currency = debit.currency
        if currency == CAD:
            cad: Decimal | None = net
            conversion = native
        elif debit.rate is not None and debit.cad is not None:
            cad = cents(net * debit.rate)
            conversion = {"cad_source": debit.cad_source, "rate": debit.rate,
                          "rate_date": debit.rate_date,
                          "conversion_state": debit.conversion_state}  # fmt: skip
        else:
            cad = None
            conversion = {"cad_source": None, "rate": None, "rate_date": None,
                          "conversion_state": "unconverted"}  # fmt: skip
    elif debit.cad is not None and credit.cad is not None:
        net = cad = debit.cad + credit.cad
        currency = CAD
        conversion = native
    else:
        return None
    if net == 0:
        return None
    return {
        "id": derived_id(debit.day, (FEE_ORIGIN, edge.id)),
        "account_id": debit.account_id,
        "posting_date": debit.day,
        "recognition_date": debit.recognized,
        "amount": net,
        "currency": currency,
        "cad_amount": cad,
        **conversion,
        "description_norm": FEE_WORDING,
        "origin": FEE_ORIGIN,
        "movement_kind": FIAT,
        "fee_type": FEE_TYPE,
    }


def resolve(uow: UnitOfWork) -> None:
    """Apply the mutually unique edges, set the transfer states and book the derived
    fee lines (module docstring)."""
    edges = _edges(uow)
    count: Counter[uuid.UUID] = Counter(end for edge in edges for end in edge.ends)
    decided = [edge for edge in edges if edge.kind != SETTLED]
    applied = [
        edge
        for edge in decided
        if edge.kind in AUTO_KINDS and all(count[end] == 1 for end in edge.ends)
    ]

    states: dict[uuid.UUID, tuple[str, str | None]] = {}
    for edge in decided:
        for end in edge.ends:
            states.setdefault(end, ("ambiguous", None))
    for edge in applied:
        for end in edge.ends:
            states[end] = ("paired", AUTO_KINDS[edge.kind])

    fees: dict[uuid.UUID, dict[str, object]] = {}
    paired_legs = [e for e in applied if e.kind == FEE_KIND and len(e.ends) == 2]
    legs = _legs(uow, {end for edge in paired_legs for end in edge.ends})
    for edge in paired_legs:
        a, b = (legs[end] for end in edge.ends)
        if a.movement_kind == FIAT and b.movement_kind == FIAT:
            fee = _fee(edge, a, b)
            if fee is not None:
                fees[edge.id] = fee
    if fees:
        uow.execute(insert(txn), list(fees.values()))
        uow.execute(
            insert(txn_source),
            [
                {
                    "txn_id": fee["id"],
                    "document_line_id": None,
                    "manual_ref": f"{FEE_REF}{edge_id}",
                    "line_key_id": fee["id"],
                    "role": "primary",
                }
                for edge_id, fee in fees.items()
            ],
        )
    if applied:
        uow.execute(
            update(pair_edge)
            .where(pair_edge.c.id == bindparam("b_id"))
            .values(applied=True, fee_txn_id=bindparam("b_fee")),
            [
                {
                    "b_id": edge.id,
                    "b_fee": fees[edge.id]["id"] if edge.id in fees else None,
                }
                for edge in applied
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
    # Every end of an edge decided here has its state now, and stage 8 stated the
    # reversal lines it settled or offered: a transfer-like line still without one has
    # no transfer candidate (the original of a same-day-only reversal among them).
    uow.execute(
        update(txn)
        .where(txn.c.transfer_like, txn.c.transfer_state.is_(None))
        .values(transfer_state="unresolved")
    )
