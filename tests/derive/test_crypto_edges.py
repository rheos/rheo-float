"""Crypto own-wallet pairing: two-sided and one-sided own transfers, the crypto labels
and the flagged withdrawal (FR-15(b), AC-59a, edge cases 24, 40 and 77; Derivation
stages 9 and 11).

Seam: ``derive.run`` through the derive ``crypto_ledger`` over documents read by the
test-only crypto fixture parser (``crypto_support``), with ``own_addresses.csv`` and
``crypto_labels.csv`` written as the owner would; read back as the txn states, the
``pair_edge`` rows and the ``event`` rows. Every address, TXID, quantity and date is
invented.
"""

from __future__ import annotations

import datetime as dt
import uuid
from decimal import Decimal

import pytest
from sqlalchemy import select

from rheo_finance.manual.accounts import ACCOUNTS_FILE, IDENTIFIERS_FILE
from rheo_finance.manual.crypto_labels import CRYPTO_LABELS_FILE
from rheo_finance.manual.own_addresses import OWN_ADDRESSES_FILE
from rheo_finance.manual.stage2 import account_id
from rheo_finance.storage.refs import format_ref
from rheo_finance.storage.schema import event, pair_edge, txn

from .crypto_support import (
    CRYPTO_INSTITUTION,
    CRYPTO_ISSUER,
    EXCHANGE_ID,
    WALLET_ID,
    crypto_document,
)
from .support import Ledger
from .test_crypto_keys import with_zone

T0 = dt.datetime(2025, 6, 10, 12, 0, tzinfo=dt.UTC)
EXCHANGE = f"ACCOUNT {EXCHANGE_ID}"
WALLET = f"ACCOUNT {WALLET_ID}"
VAULT_ID = "999000000084"
VAULT = f"ACCOUNT {VAULT_ID}"
OWN_WALLET = "0xA1b2C3d4999E5f6A7b8C9d0"
OWN_VAULT = "Sample9Vault9Address999"
FOREIGN = "Sample9Foreign9Address"
FLAGGED = "finance.withdrawal.flagged"

State = tuple[str | None, str | None]


def at(hours: float = 0) -> str:
    return (T0 + dt.timedelta(hours=hours)).isoformat()


def file(ledger: Ledger, name: str, *items: str) -> None:
    ledger.file(f"{CRYPTO_ISSUER}/{name}", crypto_document(*items))


def three_accounts(ledger: Ledger) -> None:
    """The two fixture accounts and a third wallet, ``vault``."""
    ledger.manual(
        ACCOUNTS_FILE,
        *(
            f"{slug},{slug.title()},{CRYPTO_INSTITUTION},{kind},CAD,monthly,,,,,,,,,"
            for slug, kind in (
                ("wallet", "crypto_wallet"),
                ("exchange", "exchange"),
                ("vault", "crypto_wallet"),
            )
        ),
    )
    ledger.manual(
        IDENTIFIERS_FILE,
        f"wallet,{WALLET_ID},number,,",
        f"exchange,{EXCHANGE_ID},number,,",
        f"vault,{VAULT_ID},number,,",
    )


def states(ledger: Ledger) -> dict[tuple[str, Decimal], State]:
    """Each line's (transfer state, pair kind), by its account slug and quantity."""
    slugs = {account_id(slug): slug for slug in ("wallet", "exchange", "vault")}
    rows = ledger.conn.execute(
        select(txn.c.account_id, txn.c.amount, txn.c.transfer_state, txn.c.pair_kind)
    )
    return {
        (slugs[row.account_id], row.amount): (row.transfer_state, row.pair_kind)
        for row in rows
    }


def all_states(ledger: Ledger) -> list[State]:
    """Every line's (transfer state, pair kind), sorted."""
    rows = ledger.conn.execute(select(txn.c.transfer_state, txn.c.pair_kind))
    return sorted((tuple(row) for row in rows), key=str)


def edges(ledger: Ledger) -> list[tuple[str, Decimal, Decimal | None, bool]]:
    """Each crypto edge as (kind, a's quantity, b's quantity, applied)."""
    a, b = txn.alias("a"), txn.alias("b")
    rows = ledger.conn.execute(
        select(pair_edge.c.kind, a.c.amount, b.c.amount, pair_edge.c.applied)
        .join(a, a.c.id == pair_edge.c.a_txn_id)
        .outerjoin(b, b.c.id == pair_edge.c.b_txn_id)
    )
    return sorted((tuple(row) for row in rows), key=lambda row: (row[0], str(row[1:])))


def flagged(ledger: Ledger) -> list[tuple[str, dict[str, object]]]:
    rows = ledger.conn.execute(
        select(event.c.subject_ref, event.c.data).where(event.c.type == FLAGGED)
    )
    return [(row.subject_ref, row.data) for row in rows]


def txn_id(ledger: Ledger, amount: str) -> uuid.UUID:
    found = ledger.conn.scalar(select(txn.c.id).where(txn.c.amount == Decimal(amount)))
    assert isinstance(found, uuid.UUID)
    return found


# --- two-sided own transfers ----------------------------------------------------------


@pytest.mark.parametrize(
    ("deposit_txid", "paired"),
    [
        ("9990AB12CD", True),
        ("0x9990ab12cd", True),
        ("0X9990AB12CD", True),
        ("9990ab12ce", False),
    ],
    ids=["equal", "equal-0x", "equal-0X", "unequal"],
)
def test_a_shared_txid_pairs_whatever_the_figures_and_times(
    crypto_ledger: Ledger, deposit_txid: str, paired: bool
) -> None:
    # The exchange prints the withdrawal gross with its fee; the wallet prints the
    # deposit three days later with a figure of its own. Equal TXIDs (hex, in either
    # case, with or without a leading 0x) settle the pair; unequal ones rule it out
    # even at an equal quantity.
    ledger = crypto_ledger
    file(
        ledger,
        "exchange.txt",
        EXCHANGE,
        f"WITHDRAW at={at()} asset=BTC qty=1.0 fee=0.0005 gross=1 "
        f"txid=9990ab12cd to={FOREIGN} type=Withdrawal hint=1",
    )
    file(
        ledger,
        "wallet.txt",
        WALLET,
        f"MOVE at={at(72)} asset=bitcoin qty=0.999 txid={deposit_txid} type=Received",
    )
    if not paired:
        file(
            ledger,
            "other.txt",
            WALLET,
            f"MOVE at={at(1)} asset=bitcoin qty=0.9995 txid=999tx0ef56 type=Received",
        )

    ledger.derive()

    found = states(ledger)
    if paired:
        assert found == {
            ("exchange", Decimal("-0.9995")): ("paired", "own_transfer"),
            ("exchange", Decimal("-0.0005")): (None, None),
            ("wallet", Decimal("0.999")): ("paired", "own_transfer"),
        }
        assert edges(ledger) == [
            ("own_transfer", Decimal("-0.9995"), Decimal("0.999"), True)
        ]
        assert flagged(ledger) == []
    else:
        assert found[("exchange", Decimal("-0.9995"))] == ("unresolved", None)
        assert edges(ledger) == []


@pytest.mark.parametrize(
    ("hours", "deposit", "feestated", "paired"),
    [
        (30, "0.5", False, True),
        (-6, "0.5", True, True),
        (50, "0.5", True, False),
        (2, "0.495", False, True),
        (2, "0.485", False, False),
        (2, "0.505", False, False),
        (2, "0.495", True, False),
    ],
    ids=[
        "equal-inside-window",
        "equal-printed-earlier",
        "outside-window",
        "unstated-fee-inside-tolerance",
        "unstated-fee-outside-tolerance",
        "deposit-larger",
        "stated-fee-no-tolerance",
    ],
)
def test_without_txids_a_pair_needs_the_quantity_and_the_window(
    crypto_ledger: Ledger, hours: int, deposit: str, feestated: bool, paired: bool
) -> None:
    ledger = crypto_ledger
    stated = " feestated=1" if feestated else ""
    file(
        ledger,
        "exchange.txt",
        EXCHANGE,
        f"MOVE at={at()} asset=ETH qty=-0.5 type=Withdrawal hint=1{stated}",
    )
    file(
        ledger,
        "wallet.txt",
        WALLET,
        f"MOVE at={at(hours)} asset=ethereum qty={deposit} type=Received",
    )

    ledger.derive()

    out = states(ledger)[("exchange", Decimal("-0.5"))]
    if paired:
        assert out == ("paired", "own_transfer")
        assert edges(ledger) == [
            ("own_transfer", Decimal("-0.5"), Decimal(deposit), True)
        ]
    else:
        assert out == ("unresolved", None)
        assert edges(ledger) == []


def test_an_equal_deposit_of_another_asset_or_on_the_same_account_is_no_candidate(
    crypto_ledger: Ledger,
) -> None:
    ledger = crypto_ledger
    file(
        ledger,
        "exchange.txt",
        EXCHANGE,
        f"MOVE at={at()} asset=ETH qty=-0.5 type=Withdrawal hint=1",
        f"MOVE at={at(1)} asset=ETH qty=0.5 type=Deposit",
    )
    file(ledger, "wallet.txt", WALLET, f"MOVE at={at(1)} asset=solana qty=0.5")

    ledger.derive()

    assert states(ledger)[("exchange", Decimal("-0.5"))] == ("unresolved", None)
    assert edges(ledger) == []


def test_two_candidate_deposits_leave_every_line_ambiguous(
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
        f"MOVE at={at(1)} asset=ethereum qty=0.5 type=Received",
        f"MOVE at={at(3)} asset=ethereum qty=0.5 type=Received",
    )

    ledger.derive()

    assert all_states(ledger) == [("ambiguous", None)] * 3
    assert [applied for *_, applied in edges(ledger)] == [False, False]
    assert flagged(ledger) == []


def test_an_own_address_destination_pairs_only_on_its_account(
    crypto_ledger: Ledger,
) -> None:
    # Two equal deposits on two wallets: the withdrawal's destination is the vault's
    # address, so only the vault's deposit is a candidate and the pair is unique.
    ledger = crypto_ledger
    three_accounts(ledger)
    ledger.manual(OWN_ADDRESSES_FILE, f"vault,{OWN_VAULT}")
    file(
        ledger,
        "exchange.txt",
        EXCHANGE,
        f"MOVE at={at()} asset=ETH qty=-0.5 type=Withdrawal to={OWN_VAULT}",
    )
    file(ledger, "wallet.txt", WALLET, f"MOVE at={at(1)} asset=ethereum qty=0.5")
    file(ledger, "vault.txt", VAULT, f"MOVE at={at(1)} asset=ethereum qty=0.5")

    ledger.derive()

    assert states(ledger) == {
        ("exchange", Decimal("-0.5")): ("paired", "own_transfer"),
        ("vault", Decimal("0.5")): ("paired", "own_transfer"),
        ("wallet", Decimal("0.5")): (None, None),
    }


# --- one-sided own transfers ----------------------------------------------------------


def test_a_one_sided_own_transfer_becomes_two_sided_when_the_deposit_loads(
    crypto_ledger: Ledger,
) -> None:
    # The exchange withdraws to the wallet's registered address; the wallet's rows are
    # not loaded. The withdrawal is a one-sided own transfer, with no flag. When the
    # wallet's export is filed, the next derive pairs it two sided, with no decision.
    ledger = crypto_ledger
    ledger.manual(
        OWN_ADDRESSES_FILE, f"wallet,{OWN_WALLET.upper().replace('0X', '0x')}"
    )
    file(
        ledger,
        "exchange.txt",
        EXCHANGE,
        f"WITHDRAW at={at()} asset=ETH qty=0.25 fee=0.001 to={OWN_WALLET}",
    )

    ledger.derive()

    withdrawal = txn_id(ledger, "-0.25")
    assert states(ledger)[("exchange", Decimal("-0.25"))] == (
        "paired",
        "own_one_sided",
    )
    assert edges(ledger) == [("own_one_sided", Decimal("-0.25"), None, True)]
    assert ledger.conn.scalar(select(pair_edge.c.b_account_id)) == account_id("wallet")
    assert flagged(ledger) == []

    file(ledger, "wallet.csv", WALLET, f"MOVE at={at(2)} asset=ethereum qty=0.25")
    ledger.derive()

    assert txn_id(ledger, "-0.25") == withdrawal
    assert states(ledger) == {
        ("exchange", Decimal("-0.25")): ("paired", "own_transfer"),
        ("exchange", Decimal("-0.001")): (None, None),
        ("wallet", Decimal("0.25")): ("paired", "own_transfer"),
    }
    assert edges(ledger) == [("own_transfer", Decimal("-0.25"), Decimal("0.25"), True)]
    assert flagged(ledger) == []


# --- unresolved withdrawals and labels ------------------------------------------------


def test_an_unresolved_withdrawal_raises_one_flagged_event_with_refs_only(
    crypto_ledger: Ledger,
) -> None:
    # A withdrawal to an address nobody registered, and an unmatched deposit: both
    # are unresolved, and only the withdrawal is flagged, once per derive.
    ledger = crypto_ledger
    file(
        ledger,
        "exchange.txt",
        EXCHANGE,
        f"WITHDRAW at={at()} asset=USDT qty=120 fee=1 txid=999tx0aa99 "
        f"to={FOREIGN} type=Withdrawal hint=1",
        f"MOVE at={at(5)} asset=USDT qty=40 type=Deposit hint=1",
    )

    ledger.derive()
    ledger.derive()

    assert states(ledger) == {
        ("exchange", Decimal("-120")): ("unresolved", None),
        ("exchange", Decimal("-1")): (None, None),
        ("exchange", Decimal("40")): ("unresolved", None),
    }
    ref = format_ref("finance", "txn", txn_id(ledger, "-120"))
    assert flagged(ledger) == [
        (
            ref,
            {
                "account_ref": format_ref("finance", "account", account_id("exchange")),
                "txn_ref": ref,
            },
        )
    ]


@pytest.mark.parametrize("label", ["own_untracked", "other"])
def test_a_label_takes_a_line_off_the_unresolved_count_and_the_flag(
    crypto_ledger: Ledger, label: str
) -> None:
    ledger = crypto_ledger
    file(
        ledger,
        "exchange.txt",
        EXCHANGE,
        f"MOVE at={at()} asset=LTC qty=-3 type=Withdrawal hint=1 to={FOREIGN}",
        f"MOVE at={at(1)} asset=LTC qty=-2 type=Withdrawal hint=1 to={FOREIGN}",
    )
    ledger.derive()
    assert len(flagged(ledger)) == 2

    labelled = format_ref("finance", "txn", txn_id(ledger, "-3"))
    ledger.manual(CRYPTO_LABELS_FILE, f"{labelled},{label},sample note")
    ledger.derive()

    assert states(ledger) == {
        ("exchange", Decimal("-3")): ("paired", "label"),
        ("exchange", Decimal("-2")): ("unresolved", None),
    }
    assert [ref for ref, _ in flagged(ledger)] == [
        format_ref("finance", "txn", txn_id(ledger, "-2"))
    ]


def test_a_labelled_deposit_is_no_candidate(crypto_ledger: Ledger) -> None:
    # Two equal deposits would leave the withdrawal ambiguous; the owner labels one,
    # and the other pairs on its own.
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
        f"MOVE at={at(1)} asset=ethereum qty=0.5 type=Received",
        f"MOVE at={at(3)} asset=ethereum qty=0.5 type=Airdrop",
    )
    ledger.derive()
    later = ledger.conn.scalar(
        select(txn.c.id).where(txn.c.amount > 0).order_by(txn.c.occurred_at.desc())
    )
    assert isinstance(later, uuid.UUID)
    ledger.manual(CRYPTO_LABELS_FILE, f"{format_ref('finance', 'txn', later)},other,")

    ledger.derive()

    assert all_states(ledger) == [
        ("paired", "label"),
        ("paired", "own_transfer"),
        ("paired", "own_transfer"),
    ]


def test_crypto_edge_ids_do_not_move_with_the_time_zone(crypto_ledger: Ledger) -> None:
    # Late in the UTC day, so the local posting date differs between the two zones.
    # An edge id, like a line's, is keyed to the UTC date: a zone change re-keys none.
    ledger = crypto_ledger
    late = "2025-03-10T03:30:00+00:00"
    ledger.manual(OWN_ADDRESSES_FILE, f"vault,{OWN_VAULT}")
    three_accounts(ledger)
    file(
        ledger,
        "exchange.txt",
        EXCHANGE,
        f"MOVE at={late} asset=ETH qty=-0.5 type=Withdrawal hint=1",
        f"MOVE at={late} asset=SOL qty=-4 type=Withdrawal to={OWN_VAULT}",
    )
    file(ledger, "wallet.txt", WALLET, f"MOVE at={late} asset=ethereum qty=0.5")
    ledger.derive()
    before = set(ledger.conn.scalars(select(pair_edge.c.id)))
    dates = set(ledger.conn.scalars(select(txn.c.posting_date)))

    with_zone(ledger, "Asia/Tokyo")
    ledger.derive()

    # The control: the posting dates moved, and both kinds of edge are there.
    assert set(ledger.conn.scalars(select(txn.c.posting_date))).isdisjoint(dates)
    assert sorted(ledger.conn.scalars(select(pair_edge.c.kind))) == [
        "own_one_sided",
        "own_transfer",
    ]
    assert set(ledger.conn.scalars(select(pair_edge.c.id))) == before
