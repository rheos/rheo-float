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
``own_one_sided`` edge), of any kind, in full: a ``pair`` some edge of which no stage
would store (a mixed fiat and crypto pair, two lines of one sign, a labelled crypto
leg, a one-line pair with no own address, and the rest ``pair_decisions`` lists) is
inert there, so it reaches no stage, takes no line and leaves no edge of its own. A
``pair`` that applies with an edge set no stage stored means the two disagree: stage
11 raises ``DecidedEdgeMissing`` naming the decision, never a bare error.
Every decided edge is ``applied`` with ``basis_ref`` the decision's reference
(``finance.decision:<id>``, the owner's basis), and its lines are ``paired`` with the
edge's kind as ``pair_kind``. Decisions key their lines by line key, so they hold
through a rebuild; one whose key no line carries is the orphan stage's.

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

**Fee lines.** What applied books its fee lines through ``fees.book``: the net of a
decided fiat ``pair`` (with legs, once, leaving an isolated conversion out at its legs'
CAD values, keyed to its first edge that is no such conversion and floored at zero; or
two lines joined by an ``own_transfer``, signed), the net of an applied fiat
``own_transfer`` (signed), and the spread of every applied ``conversion`` but one a
pair with legs fans out (``fees`` holds the rules).
"""

from __future__ import annotations

import uuid
from collections import Counter, defaultdict
from collections.abc import Mapping
from types import MappingProxyType
from typing import Annotated, Final

from pydantic import BaseModel, ConfigDict
from sqlalchemy import bindparam, func, select, update

from rheo_finance.context import UnitOfWork
from rheo_finance.contracts import Tiered
from rheo_finance.derive import crypto_edges, fees, pairing
from rheo_finance.derive.pair_decisions import Decided, PairDecision
from rheo_finance.derive.pairing import StoredEdge
from rheo_finance.events import record_event
from rheo_finance.parsers.base import CRYPTO_TRANSFER
from rheo_finance.storage.refs import FINANCE_SYSTEM, format_ref
from rheo_finance.storage.schema import pair_edge, txn
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


class DecidedEdgeMissing(RuntimeError):
    """A ``pair`` decision that applies has an edge set no stage stored: the decisions
    read and the stages disagree on what can be stored. A code fault, never data."""

    def __init__(self, decision_ref: str) -> None:
        super().__init__(
            f"{decision_ref}: an applied pair decision names lines no stage joined;"
            " pair_decisions and the pairing stages disagree on what can be stored"
        )
        self.decision_ref = decision_ref


def _decide(
    stored: list[StoredEdge], decided: Decided
) -> list[tuple[PairDecision, list[StoredEdge]]]:
    """Each ``pair`` decision that applies, with its edges in the order of its lines
    (module docstring)."""
    by_ends: dict[frozenset[uuid.UUID], list[StoredEdge]] = defaultdict(list)
    for edge in stored:
        by_ends[frozenset(edge.ends)].append(edge)
    # ``pair_decisions`` holds inert every ``pair`` some edge of which no stage would
    # store, so each edge set here was stored; if not, the two disagree, and that is
    # raised naming the decision. One edge per kind joins two lines; two kinds is the
    # kind order's.
    found: list[tuple[PairDecision, list[StoredEdge]]] = []
    for each in decided.pairs:
        if any(not by_ends[ends] for ends in each.edge_sets):
            raise DecidedEdgeMissing(
                format_ref(FINANCE_SYSTEM, DECISION_REF_TYPE, each.id)
            )
        found.append(
            (
                each,
                [
                    min(by_ends[ends], key=lambda e: (e.kind, e.id))
                    for ends in each.edge_sets
                ],
            )
        )
    return found


def resolve(uow: UnitOfWork, decided: Decided) -> None:
    """Apply the owner's ``pair`` decisions, then the mutually unique edges, set the
    transfer states and book the derived fee lines (module docstring)."""
    stored = pairing.stored_edges(uow)
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

    fee_ids = fees.book(uow, stored=stored, applied=applied, pairs=pairs)
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
                    "b_fee": fee_ids.get(edge.id),
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
