"""Derivation stage 11, the fee lines of what it applied (FR-15, FR-15(c), Key
decision 9, edge cases 2, 3, 5 and 70). ``stage11.resolve`` decides the edges and
calls ``book``.

**Three ways a movement books a fee**, each at most one line:

- **A decided ``pair`` that nets.** A fiat ``pair`` with legs (three lines or more)
  nets once, whatever its edges' kinds (an ``own_transfer``, a reversal-shaped edge or
  a ``conversion`` on one of its legs): the target against the sum of every other line
  it names, so a payment paid by two credits is one movement and at most one fee line,
  on the OUTBOUND side (the target when it is the debit, else the first debit among
  the others), keyed to the decision's first edge. A conversion inside such a pair
  books no spread of its own: the net already holds it. A two-line fiat ``pair``
  nets the same way when its edge is an ``own_transfer``; a two-line ``reversal`` is
  of exactly opposite amounts, and a two-line ``conversion`` books its spread (below).
  A crypto ``pair`` books no fee.
- **An applied fiat ``own_transfer``** no decision touched, whose legs do not net to
  zero (inside the tolerance), books the net.
- **An applied ``conversion``** books its spread (below).

**A net** is one txn of origin ``derived_fee`` on the OUTBOUND account (the debit's),
dated and recognized as the debit, ``fee_type`` ``fee``, with no transfer state, so it
counts once in spending (a positive net counts against it). Lines in one currency net
natively: in CAD the fee is native, in another currency it takes the debit's own
conversion (its rate, its state, unconverted when the debit is). Lines in two
currencies net by their CAD values, and the fee is that CAD figure; with a line
unconverted nothing can be measured and no fee is booked. The edge's ``fee_txn_id``
names the fee line. Its id is ``derived_id`` over the edge, and its one source is a
``manual_ref`` naming the edge (``FEE_REF``), since no document line prints it. A fee
a parser already printed is its own line before this stage and is netted out of
nothing here: a transfer whose legs balance after it makes no derived fee.

**Conversion fees** (FR-15(c)). An applied ``conversion`` makes exactly one fee line on
its date. When stage 9 named the issuer's printed fee line (``fee_txn_id``), that line
is the one and the spread is booked nowhere. A printed fee line belongs to at most one
conversion: when the conversion edges of two or more out-legs name the same one (two
conversions with the same wording on one account and day, so the line cannot be told
to be either one's), no edge keeps it, and none of those conversions books a derived
fee either, since a printed fee is the only fee line of its date. The rule reads the
whole stored edge set, so it does not depend on order. Otherwise a positive spread
becomes one ``derived_fee`` txn of ``-spread_cad`` CAD on the conversion's account,
dated and recognized as the out-leg, ``fee_type`` ``conversion_fee``, named by the
edge's ``fee_txn_id``. A spread of zero or below is a zero fee: no line, never income.
A conversion with no spread (no stored rate) makes no fee line until a rate exists.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Final, cast

from sqlalchemy import insert, select, update

from rheo_finance.context import UnitOfWork
from rheo_finance.derive.pair_decisions import PairDecision
from rheo_finance.derive.pairing import StoredEdge
from rheo_finance.derive.stage4 import cents
from rheo_finance.parsers.base import FIAT
from rheo_finance.storage.ids import derived_id
from rheo_finance.storage.schema import pair_edge, txn, txn_source

OWN_TRANSFER: Final = "own_transfer"
CONVERSION: Final = "conversion"
CONVERSION_FEE_TYPE: Final = "conversion_fee"
CONVERSION_FEE_WORDING: Final = "derived conversion fee"
CAD: Final = "CAD"
FEE_ORIGIN: Final = "derived_fee"
FEE_TYPE: Final = "fee"
FEE_WORDING: Final = "derived fee"
# A derived fee's source reference: the prefix, then the edge's id.
FEE_REF: Final = "derived_fee:"


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


def _net(key: uuid.UUID, legs: list[_Leg]) -> dict[str, object] | None:
    """The derived fee txn of one movement's lines, the target first; None when they
    net to zero or cannot be measured (module docstring). ``key`` is the edge its id
    derives from."""
    debit = next((leg for leg in legs if leg.amount < 0), legs[0])
    native: dict[str, object] = {
        "cad_source": "native", "rate": None, "rate_date": None,
        "conversion_state": "native",
    }  # fmt: skip
    if len({leg.currency for leg in legs}) == 1:
        net = sum((leg.amount for leg in legs), Decimal(0))
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
    elif all(leg.cad is not None for leg in legs):
        net = cad = sum((leg.cad for leg in legs if leg.cad is not None), Decimal(0))
        currency = CAD
        conversion = native
    else:
        return None
    if net == 0:
        return None
    return {
        "id": derived_id(debit.day, (FEE_ORIGIN, key)),
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


def _spread(edge: StoredEdge, out: _Leg) -> dict[str, object] | None:
    """The derived fee txn of one applied ``conversion``: its positive spread, when
    the issuer printed no fee (module docstring)."""
    if edge.printed_fee is not None or edge.spread is None or edge.spread <= 0:
        return None
    return {
        "id": derived_id(out.day, (FEE_ORIGIN, edge.id)),
        "account_id": out.account_id,
        "posting_date": out.day,
        "recognition_date": out.recognized,
        "amount": -edge.spread,
        "currency": CAD,
        "cad_amount": -edge.spread,
        "cad_source": "native",
        "rate": None,
        "rate_date": None,
        "conversion_state": "native",
        "description_norm": CONVERSION_FEE_WORDING,
        "origin": FEE_ORIGIN,
        "movement_kind": FIAT,
        "fee_type": CONVERSION_FEE_TYPE,
    }


def _unshare(uow: UnitOfWork, stored: Sequence[StoredEdge]) -> None:
    """Clear a printed fee line that conversion edges of two or more out-legs name
    (module docstring); each of them books no derived fee for it, since ``_spread``
    reads the edges as stored."""
    naming: dict[uuid.UUID, set[uuid.UUID]] = defaultdict(set)
    for edge in stored:
        if edge.kind == CONVERSION and edge.printed_fee is not None:
            naming[edge.printed_fee].add(edge.ends[0])
    shared = [fee for fee, outs in naming.items() if len(outs) > 1]
    if shared:
        uow.execute(
            update(pair_edge)
            .where(pair_edge.c.kind == CONVERSION, pair_edge.c.fee_txn_id.in_(shared))
            .values(fee_txn_id=None)
        )


def book(
    uow: UnitOfWork,
    *,
    stored: Sequence[StoredEdge],
    applied: Sequence[StoredEdge],
    pairs: Sequence[tuple[PairDecision, Sequence[StoredEdge]]],
) -> dict[uuid.UUID, uuid.UUID]:
    """Book the fee lines of the ``applied`` edges no decision touched and of the
    ``pairs`` that apply (each with its edges in the order of its lines), and clear
    the shared printed fees; the fee line's id by the id of the edge that names it
    (module docstring)."""
    _unshare(uow, stored)
    netted = [
        (each, found)
        for each, found in pairs
        if len(found) > 1 or found[0].kind == OWN_TRANSFER
    ]
    inside = {edge.id for _, found in netted for edge in found}
    transfers = [e for e in applied if e.kind == OWN_TRANSFER and len(e.ends) == 2]
    converted = [
        edge
        for edge in [*applied, *(e for _, found in pairs for e in found)]
        if edge.kind == CONVERSION and edge.id not in inside
    ]
    legs = _legs(
        uow,
        {end for edge in [*transfers, *converted] for end in edge.ends}
        | {line for each, _ in netted for line in each.lines},
    )
    fees: dict[uuid.UUID, dict[str, object]] = {}
    for edge in transfers:
        ends = [legs[end] for end in edge.ends]
        fee = _net(edge.id, ends) if _fiat(ends) else None
        if fee is not None:
            fees[edge.id] = fee
    for each, found in netted:
        lines = [legs[line] for line in each.lines]
        fee = _net(found[0].id, lines) if _fiat(lines) else None
        if fee is not None:
            fees[found[0].id] = fee
    for edge in converted:
        # ``a_txn_id`` is the out-leg (stage 9).
        fee = _spread(edge, legs[edge.ends[0]])
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
    return {edge_id: cast(uuid.UUID, fee["id"]) for edge_id, fee in fees.items()}


def _fiat(legs: list[_Leg]) -> bool:
    return all(leg.movement_kind == FIAT for leg in legs)
