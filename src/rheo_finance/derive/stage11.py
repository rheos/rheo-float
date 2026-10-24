"""Derivation stage 11, resolution (core part, fiat): which candidate edges apply, the
transfer states, and the derived fee lines (FR-15, Key decision 9, edge cases 2, 3, 5
and 70).

**The owner's decisions come first** (FR-15, FR-18; ``pair_decisions``, read before
stage 8). The candidate stages already built the graph around them: no edge joining
exactly an ``unpair``'s lines is stored (a loan proposal it names, "not a loan", is
gone, and a line it frees took part in the candidate search as an ordinary line), and
a line a ``pair`` took is an end of its decision's edges only, which the stages stored
even when no ordinary candidate existed. Here each ``pair`` applies its edges (the
target to each other line it names; with no other line, the target's
``own_one_sided`` edge), of any kind; a ``pair`` one of whose edges was not stored (a
crypto pair the crypto stage could not join, a one-line pair with no own address)
applies nothing. A decided edge is ``applied`` with ``basis_ref`` the decision's
reference (``finance.decision:<id>``, the owner's basis), and its lines are
``paired`` with the edge's kind as ``pair_kind``. A ``pair`` that mixes fiat and
crypto lines is inert: it stays recorded and applies nothing, since no edge kind
joins the two (``pair_decisions``). Decisions key their lines by line key, so they
hold through a rebuild; one whose key no line carries is the orphan stage's.

**Mutual uniqueness.** On the edges no decision touched, an edge applies only when it
is the only edge on both of its ends, counting the edges of every kind in
``pair_edge`` together, and its kind allows it (``AUTO_KINDS``: ``own_transfer``,
``own_one_sided`` and ``conversion`` today). The spec's stage 11 also auto-applies
``loan_out`` and ``loan_repay``, and ``trade`` when its CAD end qualifies, in this same
pass: Phase 7 adds those kinds to ``AUTO_KINDS`` here, so one count over every kind
keeps uniqueness across kinds. ``loan_partial`` and ``loan_proposal`` never apply on
their own. The result is a function of the decisions and the stored
edge set alone, so it does not depend on the order lines were imported or read.

**States.** On every line that is an end of an edge stage 11 decides:

- an end of an applied edge is ``transfer_state = paired`` with the edge's kind as
  its ``pair_kind``: it adds zero to spending and zero to income;
- any other end is ``ambiguous``, nothing applied (a confirm item);
- a transfer-like line (stage 9) left with no state is ``unresolved``: it has no edge
  of a kind decided here (an original that only a same-day reversal names included).

So each transfer-like line ends in exactly one of {paired, ambiguous, unresolved}.

**Crypto lines** (FR-15(b), edge cases 24, 40 and 77). A crypto line a
``crypto_labels.csv`` row names (``crypto_edges.labelled``; stage 9 left it out of
every pairing) is ``paired`` with ``pair_kind = label``: out of every total and off the
unresolved count. A crypto transfer-like line with no edge ends ``unresolved`` (an
unresolved crypto movement), and each WITHDRAWAL among them (kind
``crypto_transfer``, a negative quantity) raises one ``finance.withdrawal.flagged``
event, dated its posting date, whose data holds the account ref and the line ref only.
A withdrawal an applied ``own_one_sided`` edge pairs is not unresolved, so it raises
none.

**Reversals are settled already.** Stage 8 decides its ``reversal`` edges and their
lines' states. Stage 11 counts those edges (a line with a reversal edge and another
edge has two, so neither applies) but never changes a reversal edge, and a line whose
only edges are reversal edges keeps the state stage 8 gave it. A line stage 8 paired
is never a stage 9 candidate, so its one edge is its reversal edge. A reversal edge
of the owner's ``pair`` is the one stage 11 applies (above).

**Derived fee lines.** A decided ``pair`` whose edges are all fiat ``own_transfer``
edges nets once: the target against the sum of every other line it names, so a
payment paid by two credits is one movement and at most one fee line, on the
outbound side (the target when it is the debit, else the first debit among the
others), keyed to the decision's first edge. Otherwise an applied fiat
``own_transfer`` whose legs do not net to zero
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

**Conversion fees** (FR-15(c)). An applied ``conversion`` makes exactly one fee line
on its date. When stage 9 named the issuer's printed fee line (``fee_txn_id``), that
line is the one and the spread is booked nowhere. A printed fee line belongs to at
most one conversion: when the conversion edges of two or more out-legs name the same
one (two conversions with the same wording on one account and day, so the line cannot
be told to be either one's), no edge keeps it, and none of those conversions books a
derived fee either, since a printed fee is the only fee line of its date. The rule
reads the whole stored edge set, so it does not depend on order. Otherwise a positive
spread becomes
one ``derived_fee`` txn of ``-spread_cad`` CAD on the conversion's account, dated and
recognized as the out-leg, ``fee_type`` ``conversion_fee``, named by the edge's
``fee_txn_id``. A spread of zero or below is a zero fee: no line, never income. A
conversion with no spread (no stored rate) makes no fee line until a rate exists.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections import Counter, defaultdict
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from types import MappingProxyType
from typing import Annotated, Final

from pydantic import BaseModel, ConfigDict
from sqlalchemy import bindparam, func, insert, select, update

from rheo_finance.context import UnitOfWork
from rheo_finance.contracts import Tiered
from rheo_finance.derive import crypto_edges
from rheo_finance.derive.pair_decisions import Decided, PairDecision
from rheo_finance.derive.stage4 import cents
from rheo_finance.events import record_event
from rheo_finance.parsers.base import CRYPTO_TRANSFER, FIAT
from rheo_finance.storage.ids import derived_id
from rheo_finance.storage.refs import FINANCE_SYSTEM, format_ref
from rheo_finance.storage.schema import pair_edge, txn, txn_source
from rheo_finance.tier import Tier

# The edge kinds that apply on their own when mutually unique, to the ``pair_kind``
# their lines get. Phase 7 adds ``loan_out``, ``loan_repay`` and (conditionally)
# ``trade`` here, so they share this one pass and its count across kinds.
AUTO_KINDS: Final = {
    "own_transfer": "own_transfer",
    "own_one_sided": "own_one_sided",
    "conversion": "conversion",
}
# Stage 8's kind: counted, never auto-applied here (a decision may apply it).
SETTLED: Final = "reversal"
DECISION_REF_TYPE: Final = "decision"
FEE_KIND: Final = "own_transfer"
CONVERSION: Final = "conversion"
CONVERSION_FEE_TYPE: Final = "conversion_fee"
CONVERSION_FEE_WORDING: Final = "derived conversion fee"
CAD: Final = "CAD"
FEE_ORIGIN: Final = "derived_fee"
FEE_TYPE: Final = "fee"
FEE_WORDING: Final = "derived fee"
# A derived fee's source reference: the prefix, then the edge's id.
FEE_REF: Final = "derived_fee:"
LABEL: Final = "label"

# --- events ---------------------------------------------------------------------------

WITHDRAWAL_FLAGGED: Final = "finance.withdrawal.flagged"


class WithdrawalFlagged(BaseModel):
    """An unresolved crypto withdrawal: refs only, never a quantity or an address."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    account_ref: Annotated[str, Tiered(Tier.PUBLIC)]
    txn_ref: Annotated[str, Tiered(Tier.PUBLIC)]


EVENT_MODELS: Final[Mapping[str, type[BaseModel]]] = MappingProxyType(
    {WITHDRAWAL_FLAGGED: WithdrawalFlagged}
)


@dataclass(frozen=True)
class _Edge:
    id: uuid.UUID
    kind: str
    # ``a_txn_id`` first, then ``b_txn_id`` when there is one.
    ends: tuple[uuid.UUID, ...]
    spread: Decimal | None = None
    printed_fee: uuid.UUID | None = None


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
               pair_edge.c.b_txn_id, pair_edge.c.spread_cad, pair_edge.c.fee_txn_id)
    )  # fmt: skip
    return [
        _Edge(
            row.id,
            row.kind,
            tuple(end for end in (row.a_txn_id, row.b_txn_id) if end),
            row.spread_cad,
            row.fee_txn_id,
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


def _fee(key: uuid.UUID, legs: list[_Leg]) -> dict[str, object] | None:
    """The derived fee txn of one movement: an applied fiat ``own_transfer``'s two
    legs, or a decided ``pair``'s lines, the target first; None when they net to zero
    or cannot be measured (module docstring). ``key`` is the edge its id derives from.
    """
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


def _conversion_fee(edge: _Edge, out: _Leg) -> dict[str, object] | None:
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


def _decide(
    stored: list[_Edge], decided: Decided
) -> list[tuple[PairDecision, list[_Edge]]]:
    """Each ``pair`` decision that applies, with its edges in the order of its lines
    (module docstring)."""
    by_ends: dict[frozenset[uuid.UUID], list[_Edge]] = defaultdict(list)
    for edge in stored:
        by_ends[frozenset(edge.ends)].append(edge)
    found: list[tuple[PairDecision, list[_Edge]]] = []
    for each in decided.pairs:
        edges = []
        for ends in each.edge_sets:
            stored_here = by_ends.get(ends)
            if not stored_here:
                break
            # One edge per kind joins two lines; two kinds is the kind order's.
            edges.append(min(stored_here, key=lambda e: (e.kind, e.id)))
        else:
            found.append((each, edges))
    return found


def resolve(uow: UnitOfWork, decided: Decided) -> None:
    """Apply the owner's ``pair`` decisions, then the mutually unique edges, set the
    transfer states and book the derived fee lines (module docstring)."""
    stored = _edges(uow)
    pairs = _decide(stored, decided)
    by_decision = {edge.id: d.id for d, edges in pairs for edge in edges}
    edges = [edge for edge in stored if edge.id not in by_decision]
    count: Counter[uuid.UUID] = Counter(end for edge in edges for end in edge.ends)
    automatic = [edge for edge in edges if edge.kind != SETTLED]
    applied = [
        edge
        for edge in automatic
        if edge.kind in AUTO_KINDS and all(count[end] == 1 for end in edge.ends)
    ]
    chosen = [edge for _, found in pairs for edge in found]

    states: dict[uuid.UUID, tuple[str, str | None]] = {}
    for edge in automatic:
        for end in edge.ends:
            states.setdefault(end, ("ambiguous", None))
    for edge in applied + chosen:
        for end in edge.ends:
            states[end] = ("paired", AUTO_KINDS.get(edge.kind, edge.kind))
    # Stage 9 left every labelled crypto line out of every pairing, so none is an end.
    for line_id in crypto_edges.labelled(uow):
        states[line_id] = ("paired", LABEL)

    fees: dict[uuid.UUID, dict[str, object]] = {}
    transfers = [e for e in applied if e.kind == FEE_KIND and len(e.ends) == 2]
    converted = [e for e in applied + chosen if e.kind == CONVERSION]
    netted = [
        (each, found)
        for each, found in pairs
        if all(e.kind == FEE_KIND and len(e.ends) == 2 for e in found)
    ]
    legs = _legs(
        uow,
        {end for edge in transfers + converted + chosen for end in edge.ends},
    )
    for edge in transfers:
        a, b = (legs[end] for end in edge.ends)
        if a.movement_kind == FIAT and b.movement_kind == FIAT:
            fee = _fee(edge.id, [a, b])
            if fee is not None:
                fees[edge.id] = fee
    for each, found in netted:
        lines = [legs[line] for line in each.lines]
        if all(line.movement_kind == FIAT for line in lines):
            fee = _fee(found[0].id, lines)
            if fee is not None:
                fees[found[0].id] = fee
    for edge in converted:
        # ``a_txn_id`` is the out-leg (stage 9).
        fee = _conversion_fee(edge, legs[edge.ends[0]])
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
    # A printed fee line that conversion edges of two or more out-legs name is no one's
    # (module docstring); each of them already booked no derived fee for it.
    naming: dict[uuid.UUID, set[uuid.UUID]] = defaultdict(set)
    for edge in stored:
        if edge.kind == CONVERSION and edge.printed_fee is not None:
            naming[edge.printed_fee].add(edge.ends[0])
    shared = [fee for fee, outs in naming.items() if len(outs) > 1]
    if applied or chosen:
        uow.execute(
            update(pair_edge)
            .where(pair_edge.c.id == bindparam("b_id"))
            # A conversion's printed fee line, named by stage 9, stays named.
            .values(
                applied=True,
                fee_txn_id=func.coalesce(bindparam("b_fee"), pair_edge.c.fee_txn_id),
                basis_ref=bindparam("b_basis"),
            ),
            [
                {
                    "b_id": edge.id,
                    "b_fee": fees[edge.id]["id"] if edge.id in fees else None,
                    "b_basis": (
                        format_ref(
                            FINANCE_SYSTEM, DECISION_REF_TYPE, by_decision[edge.id]
                        )
                        if edge.id in by_decision
                        else None
                    ),
                }
                for edge in applied + chosen
            ],
        )
    if shared:
        uow.execute(
            update(pair_edge)
            .where(pair_edge.c.kind == CONVERSION, pair_edge.c.fee_txn_id.in_(shared))
            .values(fee_txn_id=None)
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
    _flag_withdrawals(uow)


def _flag_withdrawals(uow: UnitOfWork) -> None:
    """One ``finance.withdrawal.flagged`` per unresolved crypto withdrawal (module
    docstring)."""
    rows = uow.execute(
        select(txn.c.id, txn.c.account_id, txn.c.posting_date).where(
            txn.c.movement_kind == CRYPTO_TRANSFER,
            txn.c.amount < 0,
            txn.c.transfer_state == "unresolved",
        )
    ).all()
    for row in rows:
        txn_ref = format_ref(FINANCE_SYSTEM, "txn", row.id)
        record_event(
            uow,
            EVENT_MODELS,
            WITHDRAWAL_FLAGGED,
            txn_ref,
            row.posting_date,
            WithdrawalFlagged(
                account_ref=format_ref(FINANCE_SYSTEM, "account", row.account_id),
                txn_ref=txn_ref,
            ),
        )
