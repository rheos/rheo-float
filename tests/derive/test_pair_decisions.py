"""The owner's ``pair`` and ``unpair`` decisions (FR-15, FR-18, AC-16, AC-17, AC-19;
SPEC Derivation stages 8, 9 and 11).

Seams: ``finance.decision.record`` and ``finance.decision.reverse`` through
``operations.dispatch``, then ``derive.run`` (the derive ``ledger``) and
``finance.ledger.rebuild`` through ``dispatch``, read back as the txn states, the
``pair_edge`` rows with their kind, ``applied`` flag and ``basis_ref``, the fee lines,
and spending and income as ``ledger.totals.cad_total`` sums the lines that count.
Decisions name lines by their line key (``txn_source.line_key_id``). Every date,
amount, name, address and quantity is invented.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import uuid
from decimal import Decimal

import pytest
from pydantic import BaseModel
from sqlalchemy import select

from rheo_finance.clock import FakeClock
from rheo_finance.contracts import Role
from rheo_finance.decision import RecordOut
from rheo_finance.derive.ops import RebuildOut
from rheo_finance.derive.snapshot import txn_snapshot
from rheo_finance.manual.own_addresses import OWN_ADDRESSES_FILE
from rheo_finance.manual.stage2 import account_id
from rheo_finance.operations import dispatch
from rheo_finance.storage.refs import format_ref
from rheo_finance.storage.schema import pair_edge, txn, txn_source

from .support import (
    CARD_IDENTIFIER,
    CARD_INSTITUTION,
    CSV_IDENTIFIER,
    Ledger,
    fx_lines,
)
from .support import FX_ISSUER as FX_DIR
from .test_crypto_edges import EXCHANGE, OWN_WALLET, WALLET, at, file
from .test_overlap import file as file_card
from .test_resolution import CHEQUING, VISA, income, register, spending
from .test_reversals import OUTGOING, export
from .test_transfer_edges import BILL_PAYMENT, PAYMENT_RECEIVED, visa_export

D = dt.date
PAID, ARRIVED, LATER = D(2025, 6, 2), D(2025, 6, 4), D(2025, 6, 6)
State = tuple[str | None, str | None]
Line = tuple[dt.date, Decimal]
REVERSAL_OF_PAYMENT = OUTGOING.reversal


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
    source's line key."""
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


def decide(
    ledger: Ledger,
    kind: str,
    target: str,
    counterpart: str | None = None,
    legs: tuple[str, ...] = (),
) -> uuid.UUID:
    payload: dict[str, object] = {"kind": kind, "target_ref": target}
    if counterpart is not None:
        payload["counterpart_ref"] = counterpart
    if legs:
        payload["legs"] = [{"target_ref": leg} for leg in legs]
    out = call(ledger, "finance.decision.record", payload)
    assert isinstance(out, RecordOut)
    return out.decision_id


def reverse(ledger: Ledger, decision_id: uuid.UUID) -> None:
    call(ledger, "finance.decision.reverse", {"decision_id": str(decision_id)})


def rebuild(ledger: Ledger) -> None:
    assert isinstance(call(ledger, "finance.ledger.rebuild", {}), RebuildOut)


def states(ledger: Ledger) -> dict[Line, State]:
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


def kinds(ledger: Ledger) -> list[tuple[str, bool]]:
    rows = ledger.conn.execute(select(pair_edge.c.kind, pair_edge.c.applied))
    return sorted(tuple(row) for row in rows)


def fees(ledger: Ledger) -> list[Decimal]:
    return sorted(
        ledger.conn.scalars(select(txn.c.amount).where(txn.c.origin == "derived_fee"))
    )


def fee_rows(ledger: Ledger) -> list[tuple[uuid.UUID, Decimal]]:
    """Each derived fee line as (account, amount), sorted."""
    rows = ledger.conn.execute(
        select(txn.c.account_id, txn.c.amount).where(txn.c.origin == "derived_fee")
    )
    return sorted((tuple(row) for row in rows), key=str)


def basis(decision_id: uuid.UUID) -> str:
    return format_ref("finance", "decision", decision_id)


def transfer_like_states(ledger: Ledger) -> list[str | None]:
    return list(
        ledger.conn.scalars(select(txn.c.transfer_state).where(txn.c.transfer_like))
    )


def chequing_and_card(
    ledger: Ledger,
    bank: list[tuple[dt.date, str, str]],
    card: list[tuple[dt.date, str]],
) -> None:
    """Chequing lines ``(day, wording, amount)`` and card payment credits ``(day,
    amount)``."""
    register(ledger, CHEQUING, VISA)
    ledger.file(
        "vancity/export.csv",
        export(*((CSV_IDENTIFIER, d, w, Decimal(a)) for d, w, a in bank)),
    )
    if card:
        ledger.file(
            "vancity-visa/2025-06-30.csv",
            visa_export(*((d, PAYMENT_RECEIVED, Decimal(a)) for d, a in card)),
        )


def one_payment_two_credits(ledger: Ledger) -> None:
    """A card payment from chequing with two card credits as its candidates."""
    chequing_and_card(
        ledger,
        [(PAID, BILL_PAYMENT, "-200.00")],
        [(ARRIVED, "200.00"), (LATER, "200.00")],
    )


AMBIGUOUS: dict[Line, State] = {
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
        # The payment is taken, so the later credit has no candidate.
        (LATER, Decimal("200.00")): ("unresolved", None),
    }
    assert states(ledger) == decided
    assert edges(ledger) == [(True, basis(decision_id))]
    assert spending(ledger) == income(ledger) == Decimal("0.00")

    rebuild(ledger)

    assert states(ledger) == decided
    assert edges(ledger) == [(True, basis(decision_id))]

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
    assert edges(ledger) == [(True, basis(newer))]


def test_a_pair_across_accounts_with_no_edge_books_the_difference_as_one_fee(
    ledger: Ledger,
) -> None:
    # The card credit is outside the transfer tolerance, so no candidate edge exists
    # and both lines are unresolved. The owner's pair joins them, and the difference
    # is one fee line on the outbound account, counted once in spending.
    chequing_and_card(ledger, [(PAID, BILL_PAYMENT, "-20.00")], [(ARRIVED, "14.99")])
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
    assert fees(ledger) == [Decimal("-5.01")]
    assert spending(ledger) == Decimal("-5.01")
    assert income(ledger) == Decimal("0.00")


@pytest.mark.parametrize(
    ("credit", "kind", "fee"),
    [("50.00", "reversal", []), ("45.00", "own_transfer", [Decimal("-5.00")])],
    ids=["reversal-shaped", "unequal"],
)
def test_a_pair_on_one_account_in_one_currency_is_a_reversal_only_when_shaped_so(
    ledger: Ledger, credit: str, kind: str, fee: list[Decimal]
) -> None:
    # Neither line carries reversal wording, so stage 8 offers nothing. Exactly
    # opposite amounts pair as a reversal; any other difference is one fee line.
    register(ledger, CHEQUING)
    ledger.file(
        "vancity/export.csv",
        export(
            (CSV_IDENTIFIER, PAID, "SAMPLE SHOP 99971", Decimal("-50.00")),
            (CSV_IDENTIFIER, LATER, "SAMPLE SHOP CREDIT 99972", Decimal(credit)),
        ),
    )
    ledger.derive()

    decision_id = decide(
        ledger, "pair", key_ref(ledger, LATER, credit), key_ref(ledger, PAID, "-50.00")
    )
    ledger.derive()

    assert kinds(ledger) == [(kind, True)]
    assert edges(ledger, kind) == [(True, basis(decision_id))]
    assert set(states(ledger).values()) == {("paired", kind)}
    assert fees(ledger) == fee
    assert spending(ledger) == sum(fee, Decimal("0.00"))


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


# --- pair with legs: one movement, netted once ----------------------------------------


@pytest.mark.parametrize(
    ("second", "fee"),
    [("100.00", []), ("95.00", [Decimal("-5.00")])],
    ids=["nets-to-zero", "short-by-5"],
)
def test_a_pair_with_legs_nets_the_target_against_the_legs_once(
    ledger: Ledger, second: str, fee: list[Decimal]
) -> None:
    # One payment settled by two card credits: no candidate edge (each credit is far
    # from the payment's amount), so the lines are unresolved until the owner pairs
    # the payment with both credits as legs.
    chequing_and_card(
        ledger,
        [(PAID, BILL_PAYMENT, "-200.00")],
        [(ARRIVED, "100.00"), (LATER, second)],
    )
    ledger.derive()

    decision_id = decide(
        ledger,
        "pair",
        key_ref(ledger, PAID, "-200.00"),
        legs=(key_ref(ledger, ARRIVED, "100.00"), key_ref(ledger, LATER, second)),
    )
    ledger.derive()

    assert set(states(ledger).values()) == {("paired", "own_transfer")}
    assert edges(ledger) == [(True, basis(decision_id))] * 2
    assert fees(ledger) == fee
    assert spending(ledger) == sum(fee, Decimal("0.00"))
    assert income(ledger) == Decimal("0.00")


# --- a hand conversion and a mixed pair ----------------------------------------------


def test_a_pair_on_one_account_in_two_currencies_is_a_conversion_with_its_spread(
    fx_ledger: Ledger,
) -> None:
    # No conversion wording, so stage 9 offers no conversion. Paired by the owner, the
    # spread is the out-leg's CAD value minus the in-leg's (its stated CAD value, with
    # no rate stored), and it is one fee line on the conversion date.
    ledger = fx_ledger
    ledger.file(
        f"{FX_DIR}/cad.txt", fx_lines("CAD", "2025-06-02 -140.00 - - SAMPLE OUT 99961")
    )
    ledger.file(
        f"{FX_DIR}/usd.txt",
        fx_lines("USD", "2025-06-02 100.00 136.00 - SAMPLE IN 99962"),
    )
    ledger.derive()
    assert kinds(ledger) == []

    decision_id = decide(
        ledger,
        "pair",
        key_ref(ledger, PAID, "-140.00"),
        key_ref(ledger, PAID, "100.00"),
    )
    ledger.derive()

    assert kinds(ledger) == [("conversion", True)]
    assert edges(ledger, "conversion") == [(True, basis(decision_id))]
    assert ledger.conn.scalar(select(pair_edge.c.spread_cad)) == Decimal("4.00")
    assert fees(ledger) == [Decimal("-4.00")]
    assert set(states(ledger).values()) == {("paired", "conversion")}


def test_a_pair_of_a_fiat_and_a_crypto_line_is_inert(crypto_ledger: Ledger) -> None:
    # No edge kind joins a fiat line to a crypto line, so the decision stays recorded
    # and takes nothing: the withdrawal still pairs with its deposit.
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
    )
    ledger.cash(f"exchange,{D(2025, 6, 10)},700.00,CAD,SAMPLE SALE 99963,")
    ledger.derive()
    before = states(ledger)
    assert before[(D(2025, 6, 10), Decimal("-0.5"))] == ("paired", "own_transfer")

    decide(
        ledger,
        "pair",
        key_ref(ledger, D(2025, 6, 10), "-0.5"),
        key_ref(ledger, D(2025, 6, 10), "700.00"),
    )
    ledger.derive()

    assert states(ledger) == before
    assert edges(ledger) == [(True, None)]


# --- crypto ---------------------------------------------------------------------------


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
    first, second = sorted(
        ledger.conn.execute(
            select(txn.c.occurred_at, txn.c.id).where(txn.c.amount > 0)
        ).all()
    )

    decision_id = decide(
        ledger,
        "pair",
        key_ref(ledger, D(2025, 6, 10), "-0.5"),
        format_ref("finance", "txn", first.id),
    )
    ledger.derive()

    found = {
        row.id: row.transfer_state
        for row in ledger.conn.execute(select(txn.c.id, txn.c.transfer_state))
    }
    assert found[first.id] == "paired"
    assert found[second.id] == "unresolved"
    assert edges(ledger) == [(True, basis(decision_id))]


def test_a_pair_joins_two_crypto_lines_with_no_candidate(
    crypto_ledger: Ledger,
) -> None:
    # The deposit is far below the withdrawal's quantity, so no candidate edge
    # exists; the owner's pair joins them as one own transfer.
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
        f"MOVE at={at(1)} asset=ethereum qty=0.3 type=Received hint=1",
    )
    ledger.derive()
    assert kinds(ledger) == []

    decision_id = decide(
        ledger,
        "pair",
        key_ref(ledger, D(2025, 6, 10), "0.3"),
        key_ref(ledger, D(2025, 6, 10), "-0.5"),
    )
    ledger.derive()

    assert edges(ledger) == [(True, basis(decision_id))]
    assert set(states(ledger).values()) == {("paired", "own_transfer")}
    assert fees(ledger) == []


def test_a_one_line_pair_keeps_a_withdrawal_one_sided(crypto_ledger: Ledger) -> None:
    # The withdrawal goes to the wallet's own address and two wallet deposits match:
    # ambiguous. The owner's pair of the withdrawal alone keeps only its one-sided
    # edge, so neither deposit is its other end.
    ledger = crypto_ledger
    ledger.manual(OWN_ADDRESSES_FILE, f"wallet,{OWN_WALLET}")
    file(
        ledger,
        "exchange.txt",
        EXCHANGE,
        f"MOVE at={at()} asset=ETH qty=-0.5 type=Withdrawal to={OWN_WALLET}",
    )
    file(
        ledger,
        "wallet.txt",
        WALLET,
        f"MOVE at={at(1)} asset=ethereum qty=0.5 type=Received hint=1",
        f"MOVE at={at(3)} asset=ethereum qty=0.5 type=Received hint=1",
    )
    ledger.derive()
    assert kinds(ledger) == [("own_transfer", False)] * 2

    decision_id = decide(ledger, "pair", key_ref(ledger, D(2025, 6, 10), "-0.5"))
    ledger.derive()

    assert kinds(ledger) == [("own_one_sided", True)]
    assert edges(ledger, "own_one_sided") == [(True, basis(decision_id))]
    found = ledger.conn.execute(select(txn.c.transfer_state, txn.c.pair_kind)).all()
    assert sorted(map(str, found)) == [
        "('paired', 'own_one_sided')",
        "('unresolved', None)",
        "('unresolved', None)",
    ]


# --- unpair ---------------------------------------------------------------------------


def test_an_unpair_removes_an_edge_and_reversing_it_restores_the_pair(
    ledger: Ledger,
) -> None:
    chequing_and_card(ledger, [(PAID, BILL_PAYMENT, "-200.00")], [(ARRIVED, "200.00")])
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


def netted_card_payment(ledger: Ledger) -> None:
    """A card bill payment that stage 8 nets against a later reversal, and the card's
    credit for it, which then has no candidate."""
    chequing_and_card(
        ledger,
        [(PAID, BILL_PAYMENT, "-200.00"), (LATER, REVERSAL_OF_PAYMENT, "200.00")],
        [(ARRIVED, "200.00")],
    )
    ledger.derive()
    assert states(ledger) == {
        (PAID, Decimal("-200.00")): ("paired", "reversal"),
        (LATER, Decimal("200.00")): ("paired", "reversal"),
        (ARRIVED, Decimal("200.00")): ("unresolved", None),
    }


FREED: dict[Line, State] = {
    (PAID, Decimal("-200.00")): ("paired", "own_transfer"),
    (ARRIVED, Decimal("200.00")): ("paired", "own_transfer"),
    (LATER, Decimal("200.00")): ("unresolved", None),
}


def test_an_unpaired_reversal_frees_its_original_for_its_transfer_candidate(
    ledger: Ledger,
) -> None:
    netted_card_payment(ledger)

    decide(
        ledger,
        "unpair",
        key_ref(ledger, LATER, "200.00"),
        key_ref(ledger, PAID, "-200.00"),
    )
    ledger.derive()

    assert states(ledger) == FREED
    assert kinds(ledger) == [("own_transfer", True)]
    assert set(transfer_like_states(ledger)) <= {"paired", "ambiguous", "unresolved"}
    assert spending(ledger) == Decimal("0.00")


def test_a_pair_that_takes_one_line_of_a_reversal_leaves_the_other_unresolved(
    ledger: Ledger,
) -> None:
    netted_card_payment(ledger)

    decision_id = decide(
        ledger,
        "pair",
        key_ref(ledger, PAID, "-200.00"),
        key_ref(ledger, ARRIVED, "200.00"),
    )
    ledger.derive()

    assert states(ledger) == FREED
    assert kinds(ledger) == [("own_transfer", True)]
    assert edges(ledger) == [(True, basis(decision_id))]


@pytest.mark.parametrize(
    "unpair_first", [True, False], ids=["pair-newer", "unpair-newer"]
)
def test_an_unpair_and_a_pair_with_legs_on_one_edge_resolve_by_age(
    ledger: Ledger, unpair_first: bool
) -> None:
    chequing_and_card(
        ledger,
        [(PAID, BILL_PAYMENT, "-200.00")],
        [(ARRIVED, "200.00"), (LATER, "10.00")],
    )
    ledger.derive()
    payment = key_ref(ledger, PAID, "-200.00")
    credit = key_ref(ledger, ARRIVED, "200.00")
    small = key_ref(ledger, LATER, "10.00")

    def pair() -> uuid.UUID:
        return decide(ledger, "pair", payment, credit, legs=(small,))

    if unpair_first:
        decide(ledger, "unpair", payment, credit)
        pair_id = pair()
    else:
        pair_id = pair()
        decide(ledger, "unpair", payment, credit)
    ledger.derive()

    if unpair_first:
        assert edges(ledger) == [(True, basis(pair_id))] * 2
        assert set(states(ledger).values()) == {("paired", "own_transfer")}
    else:
        # The newer unpair holds the edge, so the older pair applies nothing.
        assert edges(ledger) == []
        assert set(states(ledger).values()) == {("unresolved", None)}


# --- the line-key contract ------------------------------------------------------------


def test_a_decision_holds_when_its_line_gets_a_new_primary_source(
    ledger: Ledger, card_parsers: None
) -> None:
    # A provisional card line is decided on; a statement then becomes its primary
    # source, so the txn's id is no longer the key the decision holds. The old key
    # stays on the txn as an additional source, and the decision still applies.
    ledger.accounts(
        ("card", CARD_INSTITUTION, "credit_card", CARD_IDENTIFIER),
        ("chequing", "Vancity", "chequing", CSV_IDENTIFIER),
    )
    # A debit and a credit: a hand pair of two debits is inert.
    ledger.cash("chequing,2025-11-10,22.00,CAD,SAMPLE CARD REFUND 99964,")
    for name in ("statement_oct", "statement_nov", "download_2025"):
        file_card(ledger, name)
    ledger.derive()
    provisional = key_ref(ledger, D(2025, 11, 8), "-22.00")
    decision_id = decide(
        ledger,
        "pair",
        provisional,
        key_ref(ledger, D(2025, 11, 10), "22.00"),
    )

    file_card(ledger, "statement_dec")
    ledger.derive()

    card_line = ledger.conn.execute(
        select(txn.c.id, txn.c.transfer_state, txn.c.provisional).where(
            txn.c.posting_date == D(2025, 11, 9), txn.c.amount == Decimal("-22.00")
        )
    ).one()
    assert format_ref("finance", "txn", card_line.id) != provisional
    assert not card_line.provisional
    assert card_line.transfer_state == "paired"
    assert edges(ledger) == [(True, basis(decision_id))]


# --- a pair with legs nets once, whatever its edges' kinds ----------------------------


def test_a_pair_with_a_reversal_shaped_leg_nets_once_on_the_outbound_side(
    ledger: Ledger,
) -> None:
    # A payment, a credit of its amount on the same account (a reversal shape, so
    # stage 8 stores that edge) and a small card credit, paired as one movement: the
    # target nets against both legs once, so the 10.00 left over is one fee line on
    # the outbound account, not lost.
    chequing_and_card(
        ledger,
        [(PAID, BILL_PAYMENT, "-200.00"), (LATER, "SAMPLE DEPOSIT 99973", "200.00")],
        [(ARRIVED, "10.00")],
    )
    ledger.derive()

    decision_id = decide(
        ledger,
        "pair",
        key_ref(ledger, PAID, "-200.00"),
        legs=(key_ref(ledger, LATER, "200.00"), key_ref(ledger, ARRIVED, "10.00")),
    )
    ledger.derive()

    assert kinds(ledger) == [("own_transfer", True), ("reversal", True)]
    assert edges(ledger, "reversal") == [(True, basis(decision_id))]
    assert edges(ledger) == [(True, basis(decision_id))]
    assert {state for state, _ in states(ledger).values()} == {"paired"}
    assert fee_rows(ledger) == [(account_id("chequing"), Decimal("10.00"))]


def test_a_pair_with_a_conversion_leg_nets_once_and_books_no_spread(
    fx_ledger: Ledger,
) -> None:
    # The target converts to USD on its own account (a conversion by hand, whose
    # spread alone would be 4.00) and a CAD credit of 2.00 comes back: one movement,
    # netted by CAD values once, so one fee line of 2.00 and no spread line.
    ledger = fx_ledger
    ledger.file(
        f"{FX_DIR}/cad.txt",
        fx_lines(
            "CAD",
            "2025-06-02 -140.00 - - SAMPLE OUT 99961",
            "2025-06-02 2.00 - - SAMPLE BACK 99970",
        ),
    )
    ledger.file(
        f"{FX_DIR}/usd.txt",
        fx_lines("USD", "2025-06-02 100.00 136.00 - SAMPLE IN 99962"),
    )
    ledger.derive()

    decision_id = decide(
        ledger,
        "pair",
        key_ref(ledger, PAID, "-140.00"),
        legs=(key_ref(ledger, PAID, "100.00"), key_ref(ledger, PAID, "2.00")),
    )
    ledger.derive()

    assert kinds(ledger) == [("conversion", True), ("own_transfer", True)]
    assert edges(ledger, "conversion") == [(True, basis(decision_id))]
    assert fee_rows(ledger) == [(account_id("fx"), Decimal("-2.00"))]
