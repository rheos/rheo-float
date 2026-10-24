"""The owner's ``pair`` and ``unpair`` decisions in stage 11 (FR-15, FR-18, AC-16,
AC-17, AC-19; SPEC Derivation stage 11).

Seams: ``finance.decision.record`` and ``finance.decision.reverse`` through
``operations.dispatch``, then ``derive.run`` (the derive ``ledger``) and
``finance.ledger.rebuild`` through ``dispatch``, read back as the txn states, the
``pair_edge`` rows with their ``applied`` flag and ``basis_ref``, and spending and
income as ``ledger.totals.cad_total`` sums the lines that count. Decisions name lines
by their line key (``txn_source.line_key_id``). Every date, amount, name and quantity
is invented.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import uuid
from decimal import Decimal

from pydantic import BaseModel
from sqlalchemy import select

from rheo_finance.clock import FakeClock
from rheo_finance.contracts import Role
from rheo_finance.decision import RecordOut
from rheo_finance.derive.ops import RebuildOut
from rheo_finance.derive.snapshot import txn_snapshot
from rheo_finance.operations import dispatch
from rheo_finance.storage.refs import format_ref
from rheo_finance.storage.schema import pair_edge, txn, txn_source

from .support import CSV_IDENTIFIER, Ledger
from .test_crypto_edges import EXCHANGE, WALLET, at, file
from .test_resolution import CHEQUING, VISA, income, register, spending
from .test_reversals import OUTGOING, export
from .test_transfer_edges import BILL_PAYMENT, PAYMENT_RECEIVED, visa_export

D = dt.date
PAID, ARRIVED, LATER = D(2025, 6, 2), D(2025, 6, 4), D(2025, 6, 6)
State = tuple[str | None, str | None]


# --- helpers --------------------------------------------------------------------------


def call(ledger: Ledger, name: str, payload: dict[str, object]) -> BaseModel:
    """``name`` through ``dispatch``, then a minute on the fake clock, so each
    decision is newer than the one before it."""
    ledger.ctx = dataclasses.replace(ledger.ctx, snapshot_provider=txn_snapshot)
    out = dispatch(name, payload, Role.OWNER, ctx=ledger.ctx)
    assert isinstance(out, BaseModel)
    assert isinstance(ledger.ctx.clock, FakeClock)
    ledger.ctx.clock.advance(dt.timedelta(minutes=1))
    return out


def key_ref(ledger: Ledger, day: dt.date, amount: str) -> str:
    """The line reference of the one line of ``amount`` on ``day``: its primary
    source's line key, never the txn id."""
    keys = ledger.conn.scalars(
        select(txn_source.c.line_key_id)
        .join(txn, txn.c.id == txn_source.c.txn_id)
        .where(
            txn.c.posting_date == day,
            txn.c.amount == Decimal(amount),
            txn_source.c.role == "primary",
        )
    ).all()
    assert len(keys) == 1
    return format_ref("finance", "txn", keys[0])


def decide(ledger: Ledger, kind: str, target: str, counterpart: str) -> uuid.UUID:
    out = call(
        ledger,
        "finance.decision.record",
        {"kind": kind, "target_ref": target, "counterpart_ref": counterpart},
    )
    assert isinstance(out, RecordOut)
    return out.decision_id


def reverse(ledger: Ledger, decision_id: uuid.UUID) -> None:
    call(ledger, "finance.decision.reverse", {"decision_id": str(decision_id)})


def rebuild(ledger: Ledger) -> None:
    assert isinstance(call(ledger, "finance.ledger.rebuild", {}), RebuildOut)


def states(ledger: Ledger) -> dict[tuple[dt.date, Decimal], State]:
    rows = ledger.conn.execute(
        select(
            txn.c.posting_date, txn.c.amount, txn.c.transfer_state, txn.c.pair_kind
        ).where(txn.c.origin != "derived_fee")
    )
    return {(row[0], row[1]): (row[2], row[3]) for row in rows}


def edges(ledger: Ledger, kind: str = "own_transfer") -> list[tuple[bool, str | None]]:
    """Each edge of ``kind`` as (applied, basis_ref), sorted."""
    rows = ledger.conn.execute(
        select(pair_edge.c.applied, pair_edge.c.basis_ref).where(
            pair_edge.c.kind == kind
        )
    )
    return sorted((tuple(row) for row in rows), key=str)


def basis(decision_id: uuid.UUID) -> str:
    return format_ref("finance", "decision", decision_id)


def one_payment_two_credits(ledger: Ledger) -> None:
    """A card payment from chequing with two card credits as its candidates."""
    register(ledger, CHEQUING, VISA)
    ledger.file(
        "vancity/export.csv",
        export((CSV_IDENTIFIER, PAID, BILL_PAYMENT, Decimal("-200.00"))),
    )
    ledger.file(
        "vancity-visa/2025-06-30.csv",
        visa_export(
            (ARRIVED, PAYMENT_RECEIVED, Decimal("200.00")),
            (LATER, PAYMENT_RECEIVED, Decimal("200.00")),
        ),
    )


AMBIGUOUS: dict[tuple[dt.date, Decimal], State] = {
    (PAID, Decimal("-200.00")): ("ambiguous", None),
    (ARRIVED, Decimal("200.00")): ("ambiguous", None),
    (LATER, Decimal("200.00")): ("ambiguous", None),
}


# --- pair: an ambiguous pair resolves, survives rebuild, reverses ---------------------


def test_a_pair_decision_resolves_an_ambiguous_pair_through_a_rebuild_and_reverses(
    ledger: Ledger,
) -> None:
    one_payment_two_credits(ledger)
    ledger.derive()
    assert states(ledger) == AMBIGUOUS

    decision_id = decide(
        ledger,
        "pair",
        key_ref(ledger, PAID, "-200.00"),
        key_ref(ledger, ARRIVED, "200.00"),
    )
    ledger.derive()

    decided = {
        (PAID, Decimal("-200.00")): ("paired", "own_transfer"),
        (ARRIVED, Decimal("200.00")): ("paired", "own_transfer"),
        # Its one candidate is taken: a transfer-like credit with no live edge.
        (LATER, Decimal("200.00")): ("unresolved", None),
    }
    assert states(ledger) == decided
    assert edges(ledger) == [(False, None), (True, basis(decision_id))]
    assert spending(ledger) == income(ledger) == Decimal("0.00")

    rebuild(ledger)

    assert states(ledger) == decided
    assert edges(ledger) == [(False, None), (True, basis(decision_id))]

    reverse(ledger, decision_id)
    ledger.derive()

    assert states(ledger) == AMBIGUOUS
    assert edges(ledger) == [(False, None), (False, None)]


def test_the_newest_of_two_pair_decisions_on_one_line_applies(ledger: Ledger) -> None:
    one_payment_two_credits(ledger)
    ledger.derive()
    payment = key_ref(ledger, PAID, "-200.00")
    decide(ledger, "pair", payment, key_ref(ledger, ARRIVED, "200.00"))
    newer = decide(ledger, "pair", payment, key_ref(ledger, LATER, "200.00"))

    ledger.derive()

    assert states(ledger) == {
        (PAID, Decimal("-200.00")): ("paired", "own_transfer"),
        (ARRIVED, Decimal("200.00")): ("unresolved", None),
        (LATER, Decimal("200.00")): ("paired", "own_transfer"),
    }
    assert edges(ledger) == [(False, None), (True, basis(newer))]


def test_a_pair_decision_joins_two_lines_with_no_edge_by_hand_and_books_the_fee(
    ledger: Ledger,
) -> None:
    # The card credit is outside the transfer tolerance, so stage 9 gives no edge and
    # both lines are unresolved. Paired by hand, the difference is a fee line on the
    # outbound account, counted once in spending.
    register(ledger, CHEQUING, VISA)
    ledger.file(
        "vancity/export.csv",
        export((CSV_IDENTIFIER, PAID, BILL_PAYMENT, Decimal("-20.00"))),
    )
    ledger.file(
        "vancity-visa/2025-06-30.csv",
        visa_export((ARRIVED, PAYMENT_RECEIVED, Decimal("14.99"))),
    )
    ledger.derive()
    assert edges(ledger) == []

    decision_id = decide(
        ledger,
        "pair",
        key_ref(ledger, ARRIVED, "14.99"),
        key_ref(ledger, PAID, "-20.00"),
    )
    ledger.derive()

    assert states(ledger) == {
        (PAID, Decimal("-20.00")): ("paired", "own_transfer"),
        (ARRIVED, Decimal("14.99")): ("paired", "own_transfer"),
    }
    assert edges(ledger) == [(True, basis(decision_id))]
    assert spending(ledger) == Decimal("-5.01")
    assert income(ledger) == Decimal("0.00")


def test_a_pair_decision_nets_a_same_day_reversal_stage_8_only_offered(
    ledger: Ledger,
) -> None:
    register(ledger, CHEQUING)
    ledger.file(
        "vancity/export.csv",
        export(
            (CSV_IDENTIFIER, PAID, OUTGOING.original, OUTGOING.amount),
            (CSV_IDENTIFIER, PAID, OUTGOING.reversal, -OUTGOING.amount),
        ),
    )
    ledger.derive()
    assert edges(ledger, "reversal") == [(False, None)]

    decision_id = decide(
        ledger,
        "pair",
        key_ref(ledger, PAID, str(-OUTGOING.amount)),
        key_ref(ledger, PAID, str(OUTGOING.amount)),
    )
    ledger.derive()

    assert set(states(ledger).values()) == {("paired", "reversal")}
    assert edges(ledger, "reversal") == [(True, basis(decision_id))]
    assert spending(ledger) == income(ledger) == Decimal("0.00")


def test_a_pair_decision_resolves_an_ambiguous_crypto_transfer(
    crypto_ledger: Ledger,
) -> None:
    ledger = crypto_ledger
    file(
        ledger,
        "exchange.txt",
        EXCHANGE,
        f"MOVE at={at()} asset=ETH qty=-0.5 type=Withdrawal hint=1",
    )
    file(
        ledger,
        "wallet.txt",
        WALLET,
        f"MOVE at={at(1)} asset=ethereum qty=0.5 type=Received hint=1",
        f"MOVE at={at(3)} asset=ethereum qty=0.5 type=Received hint=1",
    )
    ledger.derive()
    day = D(2025, 6, 10)
    first, second = sorted(
        ledger.conn.execute(
            select(txn.c.occurred_at, txn.c.id).where(txn.c.amount > 0)
        ).all()
    )

    decision_id = decide(
        ledger,
        "pair",
        key_ref(ledger, day, "-0.5"),
        format_ref("finance", "txn", first.id),
    )
    ledger.derive()

    found = {
        row.id: row.transfer_state
        for row in ledger.conn.execute(select(txn.c.id, txn.c.transfer_state))
    }
    assert found[first.id] == "paired"
    assert found[second.id] == "unresolved"
    assert edges(ledger) == [(False, None), (True, basis(decision_id))]


# --- unpair: an edge removed --------------------------------------------------------


def test_an_unpair_removes_an_edge_and_reversing_it_restores_the_pair(
    ledger: Ledger,
) -> None:
    register(ledger, CHEQUING, VISA)
    ledger.file(
        "vancity/export.csv",
        export((CSV_IDENTIFIER, PAID, BILL_PAYMENT, Decimal("-200.00"))),
    )
    ledger.file(
        "vancity-visa/2025-06-30.csv",
        visa_export((ARRIVED, PAYMENT_RECEIVED, Decimal("200.00"))),
    )
    ledger.derive()
    assert edges(ledger) == [(True, None)]

    decision_id = decide(
        ledger,
        "unpair",
        key_ref(ledger, PAID, "-200.00"),
        key_ref(ledger, ARRIVED, "200.00"),
    )
    ledger.derive()

    assert edges(ledger) == []
    assert set(states(ledger).values()) == {("unresolved", None)}

    rebuild(ledger)
    assert edges(ledger) == []

    reverse(ledger, decision_id)
    ledger.derive()

    assert edges(ledger) == [(True, None)]
    assert set(states(ledger).values()) == {("paired", "own_transfer")}


def test_an_unpair_makes_a_line_eligible_for_its_other_edge(ledger: Ledger) -> None:
    one_payment_two_credits(ledger)
    ledger.derive()

    decide(
        ledger,
        "unpair",
        key_ref(ledger, PAID, "-200.00"),
        key_ref(ledger, LATER, "200.00"),
    )
    ledger.derive()

    assert states(ledger) == {
        (PAID, Decimal("-200.00")): ("paired", "own_transfer"),
        (ARRIVED, Decimal("200.00")): ("paired", "own_transfer"),
        (LATER, Decimal("200.00")): ("unresolved", None),
    }
    # The remaining edge applied on its own: no decision is its basis.
    assert edges(ledger) == [(True, None)]


def test_an_unpair_of_a_netted_reversal_restates_both_lines(ledger: Ledger) -> None:
    register(ledger, CHEQUING)
    ledger.file(
        "vancity/export.csv",
        export(
            (CSV_IDENTIFIER, PAID, OUTGOING.original, OUTGOING.amount),
            (CSV_IDENTIFIER, LATER, OUTGOING.reversal, -OUTGOING.amount),
        ),
    )
    ledger.derive()
    assert edges(ledger, "reversal") == [(True, None)]

    decide(
        ledger,
        "unpair",
        key_ref(ledger, LATER, str(-OUTGOING.amount)),
        key_ref(ledger, PAID, str(OUTGOING.amount)),
    )
    ledger.derive()

    assert edges(ledger, "reversal") == []
    assert states(ledger) == {
        (PAID, OUTGOING.amount): (None, None),
        (LATER, -OUTGOING.amount): ("unresolved", None),
    }
    assert spending(ledger) == OUTGOING.amount
