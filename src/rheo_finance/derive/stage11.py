"""Derivation stage 11, resolution (core part, fiat): which candidate edges apply, the
transfer states, and the derived fee lines (FR-15, Key decision 9, edge cases 2, 3, 5
and 70).

**The owner's decisions come first** (FR-15, FR-18; ``pair_decisions``). Each
``unpair`` deletes the edges joining exactly the lines it names (the target and the
counterpart; the target's ``own_one_sided`` edge when it names one line), of any
kind, a ``reversal`` included: a line it frees is eligible for its other edges, and a
loan proposal it names ("not a loan") is gone. Then each ``pair``, newest first,
applies the edge joining its target to each other line it names (the counterpart and
each leg; with no other line, the target's ``own_one_sided`` edge), of any kind; when
two lines it names have no edge, it joins them by hand with a new edge, applied:
``own_transfer`` across two accounts, ``conversion`` on one account in two currencies
(no spread, so no derived fee), ``reversal`` on one account in one currency (the later
line its ``a_txn_id``). A pair naming a line that a newer pair already took, or a lone
line with no ``own_one_sided`` edge, applies nothing. A decided edge is ``applied``
with ``basis_ref`` the decision's reference (``finance.decision:<id>``, the owner's
basis), and its lines are ``paired`` with the edge's kind as ``pair_kind``.
Every other edge on a line a decision took is out of the graph for this derive:
stored, not applied (a reversal edge stage 8 applied is un-applied), and not counted
on its other end, so that end resolves on its remaining edges. A line that loses its
reversal edges this way and has none left is restated: a reversal line
``unresolved``, an original an ordinary line again. Decisions key their lines by line
key, so they hold through a rebuild; one whose key no line carries is the orphan
stage's, and applies nothing here.

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
is never a stage 9 candidate, so its one edge is its reversal edge. A decision is the
one exception (above). shortcut: a line that keeps reversal edges after a decision
removed one of them keeps the state stage 8 gave its group (ambiguous, out of every
total) rather than having stage 8's rule run again on the smaller group; upgrade when
a real case needs the remaining edge to net without a decision.

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
from sqlalchemy import bindparam, delete, func, insert, select, update

from rheo_finance.context import UnitOfWork
from rheo_finance.contracts import Tiered
from rheo_finance.decision import DecisionKind
from rheo_finance.derive import crypto_edges
from rheo_finance.derive.pair_decisions import LineDecision, line_decisions
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
# Stage 8's kind: counted, never auto-applied here (a decision may apply or delete it).
SETTLED: Final = "reversal"
OWN_TRANSFER: Final = "own_transfer"
ONE_SIDED: Final = "own_one_sided"
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
    applied: bool = False


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
               pair_edge.c.b_txn_id, pair_edge.c.spread_cad, pair_edge.c.fee_txn_id,
               pair_edge.c.applied)
    )  # fmt: skip
    return [
        _Edge(
            row.id,
            row.kind,
            tuple(end for end in (row.a_txn_id, row.b_txn_id) if end),
            row.spread_cad,
            row.fee_txn_id,
            row.applied,
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


@dataclass(frozen=True)
class _Decided:
    """What the owner's decisions did to the stored edges (module docstring)."""

    # Each decided edge, with the decision that applied it.
    applied: list[tuple[_Edge, uuid.UUID]]
    # The edges a decision joined by hand, not stored yet.
    new: list[_Edge]
    # The ids of the edges an ``unpair`` deleted.
    deleted: set[uuid.UUID]
    # The lines a ``pair`` took.
    taken: set[uuid.UUID]


def _by_hand(a: _Leg, b: _Leg) -> _Edge:
    """The edge a ``pair`` decision joins two lines with when they have none."""
    if a.account_id != b.account_id:
        kind = OWN_TRANSFER
    elif a.currency != b.currency:
        kind = CONVERSION
    else:
        kind = SETTLED
    if kind == SETTLED:
        first, second = sorted((a, b), key=lambda leg: (leg.day, leg.id), reverse=True)
    else:
        first, second = (a, b) if a.amount < 0 else (b, a)
    edge_id = derived_id(first.day, ("pair_edge", kind, first.id, second.id))
    return _Edge(edge_id, kind, (first.id, second.id))


def _decide(
    uow: UnitOfWork, stored: list[_Edge], decisions: list[LineDecision]
) -> _Decided:
    """Apply the ``unpair`` decisions, then the ``pair`` decisions newest first, to the
    stored edges (module docstring)."""
    by_ends: dict[frozenset[uuid.UUID], list[_Edge]] = defaultdict(list)
    for edge in stored:
        by_ends[frozenset(edge.ends)].append(edge)
    deleted = {
        edge.id
        for each in decisions
        if each.kind is DecisionKind.UNPAIR
        for edge in by_ends.get(frozenset(each.lines), ())
    }
    pairs = [each for each in decisions if each.kind is DecisionKind.PAIR]
    legs = _legs(uow, {line for each in pairs for line in each.lines})
    decided = _Decided([], [], deleted, set())
    for each in pairs:
        if decided.taken & set(each.lines):
            continue
        target, *others = each.lines
        chosen: list[tuple[_Edge, bool]] = []
        for ends in [frozenset({target, other}) for other in others] or [
            frozenset({target})
        ]:
            found = [e for e in by_ends.get(ends, ()) if e.id not in deleted]
            if found:
                # One edge per kind joins two lines; two kinds is the kind order's.
                chosen.append((min(found, key=lambda e: (e.kind, e.id)), False))
            elif len(ends) == 2:
                chosen.append((_by_hand(*(legs[line] for line in sorted(ends))), True))
            else:
                break
        else:
            decided.taken.update(each.lines)
            decided.applied.extend((edge, each.id) for edge, _ in chosen)
            decided.new.extend(edge for edge, new in chosen if new)
    return decided


def resolve(uow: UnitOfWork) -> None:
    """Apply the owner's decisions, then the mutually unique edges, set the transfer
    states and book the derived fee lines (module docstring)."""
    stored = _edges(uow)
    decided = _decide(uow, stored, line_decisions(uow))
    by_decision = {edge.id: decision_id for edge, decision_id in decided.applied}
    # An edge a decision removed from the graph: deleted, or on a line a pair took.
    out = [
        edge
        for edge in stored
        if edge.id not in by_decision
        and (edge.id in decided.deleted or decided.taken & set(edge.ends))
    ]
    gone = {edge.id for edge in out}
    edges = [e for e in stored if e.id not in by_decision and e.id not in gone]
    count: Counter[uuid.UUID] = Counter(end for edge in edges for end in edge.ends)
    automatic = [edge for edge in edges if edge.kind != SETTLED]
    applied = [
        edge
        for edge in automatic
        if edge.kind in AUTO_KINDS and all(count[end] == 1 for end in edge.ends)
    ] + [edge for edge, _ in decided.applied]

    states: dict[uuid.UUID, tuple[str | None, str | None]] = {}
    for edge in automatic:
        for end in edge.ends:
            states.setdefault(end, ("ambiguous", None))
    for edge in applied:
        for end in edge.ends:
            states[end] = ("paired", AUTO_KINDS.get(edge.kind, edge.kind))
    # Stage 9 left every labelled crypto line out of every pairing, so none is an end.
    for line_id in crypto_edges.labelled(uow):
        states[line_id] = ("paired", LABEL)
    # A line that lost its reversal edges to a decision and has no edge left.
    kept = {end for edge in edges if edge.kind == SETTLED for end in edge.ends}
    for edge in out:
        if edge.kind != SETTLED:
            continue
        for place, end in enumerate(edge.ends):
            if end not in states and end not in kept:
                states[end] = ("unresolved", None) if place == 0 else (None, None)

    if decided.new:
        uow.execute(
            insert(pair_edge),
            [
                {
                    "id": edge.id,
                    "kind": edge.kind,
                    "a_txn_id": edge.ends[0],
                    "b_txn_id": edge.ends[1],
                    "applied": False,
                }
                for edge in decided.new
            ],
        )
    fees: dict[uuid.UUID, dict[str, object]] = {}
    paired_legs = [e for e in applied if e.kind == FEE_KIND and len(e.ends) == 2]
    converted = [e for e in applied if e.kind == CONVERSION]
    legs = _legs(uow, {end for edge in paired_legs + converted for end in edge.ends})
    for edge in paired_legs:
        a, b = (legs[end] for end in edge.ends)
        if a.movement_kind == FIAT and b.movement_kind == FIAT:
            fee = _fee(edge, a, b)
            if fee is not None:
                fees[edge.id] = fee
    for edge in converted:
        # ``a_txn_id`` is the out-leg (stage 9, and ``_by_hand``).
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
    if applied:
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
                for edge in applied
            ],
        )
    if decided.deleted:
        uow.execute(delete(pair_edge).where(pair_edge.c.id.in_(decided.deleted)))
    unapplied = [e.id for e in out if e.applied and e.id not in decided.deleted]
    if unapplied:
        uow.execute(
            update(pair_edge).where(pair_edge.c.id.in_(unapplied)).values(applied=False)
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
