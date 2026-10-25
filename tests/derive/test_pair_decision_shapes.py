"""Which ``pair`` decisions apply: in full or not at all (FR-15, FR-18; SPEC Derivation
stages 8, 9 and 11).

A ``pair`` some edge of which no stage would store is inert: it stays recorded,
applies nothing, takes no line and leaves no edge of its own, so its lines resolve as
if it were absent. Seams as in ``test_pair_decisions``: ``finance.decision.record``
through ``dispatch``, then ``derive.run``, read back as the ``pair_edge`` rows, the
states and the fee lines. Every date, amount, address and quantity is invented.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from sqlalchemy import update

from rheo_finance.context import UnitOfWork
from rheo_finance.derive import stage7
from rheo_finance.manual.crypto_labels import CRYPTO_LABELS_FILE
from rheo_finance.manual.own_addresses import OWN_ADDRESSES_FILE
from rheo_finance.storage.refs import format_ref
from rheo_finance.storage.schema import txn

from .support import CSV_IDENTIFIER, Ledger, fx_lines
from .support import FX_ISSUER as FX_DIR
from .test_crypto_edges import (
    EXCHANGE,
    FOREIGN,
    OWN_WALLET,
    VAULT,
    WALLET,
    at,
    file,
    flagged,
    three_accounts,
    txn_id,
)
from .test_pair_decisions import (
    ARRIVED,
    LATER,
    PAID,
    D,
    basis,
    chequing_and_card,
    decide,
    edges,
    fees,
    key_ref,
    kinds,
    states,
)
from .test_resolution import CHEQUING, register, spending
from .test_reversals import export
from .test_transfer_edges import BILL_PAYMENT

DAY = D(2025, 6, 10)
SAMPLE_PARTY = uuid.UUID("99999999-0000-4000-8000-000000000001")


def party_line(monkeypatch: pytest.MonkeyPatch, amount: str) -> None:
    """Stand in for the peer-money attribution of stage 7 (a feature call not built
    yet): after it, the one line of ``amount`` is a party's line."""
    attribute = stage7.attribute

    def attributed(uow: UnitOfWork) -> None:
        attribute(uow)
        uow.execute(
            update(txn)
            .where(txn.c.amount == Decimal(amount))
            .values(party_id=SAMPLE_PARTY)
        )

    monkeypatch.setattr(stage7, "attribute", attributed)


# --- W1: a pair that cannot apply in full applies nothing -----------------------------


@pytest.mark.parametrize("leg", ["labelled", "swap"])
def test_a_crypto_pair_with_a_leg_no_stage_joins_leaves_no_edge(
    crypto_ledger: Ledger, leg: str
) -> None:
    # The deposit is far below the withdrawal, so no candidate joins them; the third
    # line is a labelled deposit or one leg of a swap, which no pairing reads. The
    # pair cannot apply in full, so it builds nothing: the withdrawal stays an
    # unresolved, flagged movement, never one auto-applied with no basis.
    ledger = crypto_ledger
    three_accounts(ledger)
    file(
        ledger,
        "exchange.txt",
        EXCHANGE,
        f"MOVE at={at()} asset=ETH qty=-0.5 type=Withdrawal hint=1 to={FOREIGN}",
    )
    file(
        ledger,
        "wallet.txt",
        WALLET,
        f"MOVE at={at(1)} asset=ethereum qty=0.3 type=Received hint=1",
    )
    if leg == "labelled":
        file(
            ledger,
            "vault.txt",
            VAULT,
            f"MOVE at={at(2)} asset=ethereum qty=0.2 type=Received hint=1",
        )
    else:
        file(
            ledger,
            "vault.txt",
            VAULT,
            f"MOVE at={at(2)} asset=ethereum qty=0.2 trade=999sw01",
            f"MOVE at={at(2)} asset=BTC qty=-0.01 trade=999sw01",
        )
    ledger.derive()
    if leg == "labelled":
        labelled = format_ref("finance", "txn", txn_id(ledger, "0.2"))
        ledger.manual(CRYPTO_LABELS_FILE, f"{labelled},other,sample note")
        ledger.derive()

    decide(
        ledger,
        "pair",
        key_ref(ledger, DAY, "-0.5"),
        key_ref(ledger, DAY, "0.3"),
        legs=(key_ref(ledger, DAY, "0.2"),),
    )
    ledger.derive()

    assert kinds(ledger) == []
    found = states(ledger)
    assert found[(DAY, Decimal("-0.5"))] == ("unresolved", None)
    assert found[(DAY, Decimal("0.3"))] == ("unresolved", None)
    assert len(flagged(ledger)) == 1


def test_a_fiat_pair_with_a_zero_leg_leaves_no_edge(fx_ledger: Ledger) -> None:
    # A conversion by hand plus a zero line, which no pairing reads: the pair cannot
    # apply in full, so the conversion it would have built is not in the graph at
    # all, and no spread is booked.
    ledger = fx_ledger
    ledger.file(
        f"{FX_DIR}/cad.txt",
        fx_lines(
            "CAD",
            "2025-06-02 -140.00 - - SAMPLE OUT 99961",
            "2025-06-02 0.00 - - SAMPLE ZERO 99967",
        ),
    )
    ledger.file(
        f"{FX_DIR}/usd.txt",
        fx_lines("USD", "2025-06-02 100.00 136.00 - SAMPLE IN 99962"),
    )
    ledger.derive()

    decide(
        ledger,
        "pair",
        key_ref(ledger, PAID, "-140.00"),
        key_ref(ledger, PAID, "100.00"),
        legs=(key_ref(ledger, PAID, "0.00"),),
    )
    ledger.derive()

    assert kinds(ledger) == []
    assert fees(ledger) == []
    assert set(states(ledger).values()) == {(None, None)}


def test_a_fiat_pair_with_a_party_leg_stage_9_skips_leaves_no_edge(
    ledger: Ledger, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The card credit is outside the tolerance (no candidate); the small credit is a
    # party's line, which stage 9 never reads, and it is no reversal of the payment.
    # The pair cannot apply in full: no edge, no fee, the payment unresolved.
    party_line(monkeypatch, "10.00")
    chequing_and_card(
        ledger,
        [(PAID, BILL_PAYMENT, "-210.00")],
        [(ARRIVED, "200.00"), (LATER, "10.00")],
    )
    ledger.derive()

    decide(
        ledger,
        "pair",
        key_ref(ledger, PAID, "-210.00"),
        key_ref(ledger, ARRIVED, "200.00"),
        legs=(key_ref(ledger, LATER, "10.00"),),
    )
    ledger.derive()

    assert kinds(ledger) == []
    assert fees(ledger) == []
    assert states(ledger)[(PAID, Decimal("-210.00"))] == ("unresolved", None)


def test_a_reversal_shaped_pair_with_a_party_line_applies(
    ledger: Ledger, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Stage 8 reads a party's line, so a pair of reversal shape joins it.
    party_line(monkeypatch, "50.00")
    register(ledger, CHEQUING)
    ledger.file(
        "vancity/export.csv",
        export(
            (CSV_IDENTIFIER, PAID, "SAMPLE SHOP 99971", Decimal("-50.00")),
            (CSV_IDENTIFIER, LATER, "SAMPLE DEPOSIT 99972", Decimal("50.00")),
        ),
    )
    ledger.derive()

    decision_id = decide(
        ledger, "pair", key_ref(ledger, LATER, "50.00"), key_ref(ledger, PAID, "-50.00")
    )
    ledger.derive()

    assert edges(ledger, "reversal") == [(True, basis(decision_id))]
    assert set(states(ledger).values()) == {("paired", "reversal")}


# --- W3: a hand pair needs a debit and a credit, or one asset in two directions -------


def test_a_fiat_pair_of_two_debits_is_inert(ledger: Ledger) -> None:
    register(ledger, CHEQUING)
    ledger.file(
        "vancity/export.csv",
        export(
            (CSV_IDENTIFIER, PAID, "SAMPLE SHOP 99971", Decimal("-30.00")),
            (CSV_IDENTIFIER, LATER, "SAMPLE SHOP 99972", Decimal("-20.00")),
        ),
    )
    ledger.derive()

    decide(
        ledger,
        "pair",
        key_ref(ledger, PAID, "-30.00"),
        key_ref(ledger, LATER, "-20.00"),
    )
    ledger.derive()

    assert kinds(ledger) == []
    assert fees(ledger) == []
    assert set(states(ledger).values()) == {(None, None)}
    assert spending(ledger) == Decimal("-50.00")


@pytest.mark.parametrize(
    "other",
    [
        f"MOVE at={at(1)} asset=ethereum qty=-0.3 type=Withdrawal hint=1",
        f"MOVE at={at(1)} asset=solana qty=0.5 type=Received hint=1",
    ],
    ids=["one-direction", "two-assets"],
)
def test_a_crypto_pair_needs_two_directions_and_one_asset(
    crypto_ledger: Ledger, other: str
) -> None:
    ledger = crypto_ledger
    file(
        ledger,
        "exchange.txt",
        EXCHANGE,
        f"MOVE at={at()} asset=ETH qty=-0.5 type=Withdrawal hint=1",
    )
    file(ledger, "wallet.txt", WALLET, other)
    ledger.derive()
    amount = other.split("qty=")[1].split()[0]

    decide(ledger, "pair", key_ref(ledger, DAY, "-0.5"), key_ref(ledger, DAY, amount))
    ledger.derive()

    assert kinds(ledger) == []


# --- W4: a one-line crypto pair needs a withdrawal to an own address ------------------


@pytest.mark.parametrize("target", ["-0.5", "0.5"], ids=["no-destination", "deposit"])
def test_a_one_line_crypto_pair_without_an_own_address_is_inert(
    crypto_ledger: Ledger, target: str
) -> None:
    # The withdrawal names no own address, so it has no one-sided edge; a one-line
    # pair of it (or of the deposit) is inert and the two-sided candidate stands.
    ledger = crypto_ledger
    ledger.manual(OWN_ADDRESSES_FILE, f"wallet,{OWN_WALLET}")
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
    ledger.derive()

    decide(ledger, "pair", key_ref(ledger, DAY, target))
    ledger.derive()

    assert edges(ledger) == [(True, None)]
    assert set(states(ledger).values()) == {("paired", "own_transfer")}
