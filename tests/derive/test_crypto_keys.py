"""Crypto core: the crypto natural keys, the withdrawal and its fee line, the asset
codes, and stage 7's exclusions and movement kinds (FR-4, FR-13, FR-15(b), FR-41;
Derivation stages 1, 3 and 7; Key decision 5; edge case 48; AC-72 key stability).

Seam: ``derive.run`` through the derive ``crypto_ledger`` over documents read by the
test-only crypto fixture parser (``crypto_support``), with ``own_addresses.csv`` and
``include`` decisions recorded through ``finance.decision.record``; read back as the
txns, their sources, the source discrepancies and the decisions' line keys. Every
address, TXID, quantity and date is invented.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import typing
import uuid
from collections.abc import Callable
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import BaseModel
from sqlalchemy import select

from rheo_finance.contracts import Role
from rheo_finance.decision import RecordOut
from rheo_finance.derive.snapshot import txn_snapshot
from rheo_finance.documents import issuers
from rheo_finance.manual.accounts import ACCOUNTS_FILE, IDENTIFIERS_FILE
from rheo_finance.manual.own_addresses import OWN_ADDRESSES_FILE
from rheo_finance.operations import dispatch
from rheo_finance.parsers import registry
from rheo_finance.parsers.base import CRYPTO_LINE_KINDS, LineKind
from rheo_finance.settings import parse_settings
from rheo_finance.storage.refs import format_ref
from rheo_finance.storage.schema import (
    document_line,
    source_discrepancy,
    txn,
    txn_source,
)

from .crypto_support import (
    CRYPTO_HEADER,
    CRYPTO_INSTITUTION,
    CRYPTO_ISSUER,
    CRYPTO_PARSER_PREFIX,
    EXCHANGE_ID,
    WALLET_ID,
    crypto_document,
)
from .support import Ledger

SRC = Path(__file__).resolve().parents[2] / "src"
# 03:30 UTC on 2025-03-10: the evening of 2025-03-09 in the default zone (UTC-7).
LATE = "2025-03-10T03:30:00+00:00"
NOON = "2025-03-12T12:00:00+00:00"
WALLET = f"ACCOUNT {WALLET_ID}"
EXCHANGE = f"ACCOUNT {EXCHANGE_ID}"
OWN_HEX = "0xAbCdEf999a1B2c3D"
FOREIGN = "Sample9Foreign9Address"


def file(ledger: Ledger, name: str, *items: str) -> None:
    ledger.file(f"{CRYPTO_ISSUER}/{name}", crypto_document(*items))


def rows(ledger: Ledger) -> list[dict[str, object]]:
    return ledger.txns()


def sources(ledger: Ledger, txn_id: object) -> list[str]:
    return sorted(
        ledger.conn.scalars(
            select(txn_source.c.role).where(txn_source.c.txn_id == txn_id)
        )
    )


def with_zone(ledger: Ledger, zone: str) -> None:
    ledger.ctx = dataclasses.replace(
        ledger.ctx,
        settings=parse_settings(
            f'[finance.paths]\narchive = "{ledger.archive}"\n'
            f'downloads = "{ledger.archive.parent / "downloads-unset"}"\n'
            f'[finance.locale]\ntimezone = "{zone}"\n'
        ),
    )


def record(ledger: Ledger, name: str, payload: dict[str, object]) -> BaseModel:
    ledger.ctx = dataclasses.replace(ledger.ctx, snapshot_provider=txn_snapshot)
    out = dispatch(name, payload, Role.OWNER, ctx=ledger.ctx)
    assert isinstance(out, BaseModel)
    return out


def line_ref(txn_id: object) -> str:
    assert isinstance(txn_id, uuid.UUID)
    return format_ref("finance", "txn", txn_id)


# --- stage 3: keys and ids ------------------------------------------------------------


def test_a_quantity_printed_two_ways_is_one_line_and_one_id(
    crypto_ledger: Ledger,
) -> None:
    # The same received line with no TXID, printed 1E-7 in one file, 0.0000001 in
    # another and 0.00000010000 in a third: one key, one txn with three sources, and
    # the same id whichever file is alone.
    ledger = crypto_ledger
    move = f"MOVE at={NOON} asset=bitcoin type=Received from={FOREIGN}"
    file(ledger, "first.txt", WALLET, f"{move} qty=1E-7")
    ledger.derive()
    [alone] = rows(ledger)

    file(ledger, "second.csv", WALLET, f"{move} qty=0.0000001")
    file(ledger, "third.csv", WALLET, f"{move} qty=0.00000010000")
    ledger.derive()

    [both] = rows(ledger)
    assert both["id"] == alone["id"]
    assert both["amount"] == Decimal("1E-7")
    assert sources(ledger, both["id"]) == ["additional", "additional", "primary"]
    assert ledger.conn.scalars(select(source_discrepancy.c.kind)).all() == []

    for name in ("first.txt", "second.csv"):
        (ledger.archive / CRYPTO_ISSUER / name).unlink()
    ledger.derive()
    [other] = rows(ledger)
    assert other["id"] == alone["id"]


def test_changing_the_time_zone_moves_the_posting_date_and_no_id(
    crypto_ledger: Ledger,
) -> None:
    # One line with a TXID and one without, late in the UTC day. The ids and the line
    # keys a decision holds are UTC; the posting date is the local date.
    ledger = crypto_ledger
    file(
        ledger,
        "late.txt",
        WALLET,
        f"MOVE at={LATE} asset=solana qty=2.5 txid=999tx0a1b2c3 hint=1",
        f"MOVE at={LATE} asset=ethereum qty=-0.25 type=Sent to={FOREIGN}",
    )
    ledger.derive()
    before = {row["id"]: row["posting_date"] for row in rows(ledger)}
    keys = set(ledger.conn.scalars(select(txn_source.c.line_key_id)))

    with_zone(ledger, "Asia/Tokyo")
    ledger.derive()

    after = {row["id"]: row["posting_date"] for row in rows(ledger)}
    assert set(after) == set(before)
    assert set(before.values()) == {dt.date(2025, 3, 9)}
    assert set(after.values()) == {dt.date(2025, 3, 10)}
    assert set(ledger.conn.scalars(select(txn_source.c.line_key_id))) == keys


@pytest.mark.parametrize("order", ["email-first", "export-first"])
def test_gross_and_net_lines_of_one_txid_are_one_txn_with_a_discrepancy(
    crypto_ledger: Ledger, order: str
) -> None:
    # An email (a text file) prints the deposit gross, an export (a CSV, which ranks
    # higher) prints it net. One txn with two sources; the export's quantity stands
    # and the email's line is a quantity discrepancy.
    ledger = crypto_ledger
    deposit = f"MOVE at={NOON} asset=USDT txid=999tx0d4e5f6 hint=1"
    steps: list[Callable[[], None]] = [
        lambda: file(ledger, "email.txt", EXCHANGE, f"{deposit} qty=250.00"),
        lambda: file(ledger, "export.csv", EXCHANGE, f"{deposit} qty=249.50"),
    ]
    for step in steps if order == "email-first" else steps[::-1]:
        step()

    ledger.derive()

    [deposit_txn] = rows(ledger)
    assert deposit_txn["amount"] == Decimal("249.50")
    assert deposit_txn["currency"] == "USDT"
    assert sources(ledger, deposit_txn["id"]) == ["additional", "primary"]
    found = ledger.conn.execute(
        select(source_discrepancy.c.kind, source_discrepancy.c.document_line_id)
    ).all()
    assert [kind for kind, _ in found] == ["quantity_mismatch"]
    email_line = ledger.conn.scalar(
        select(txn_source.c.document_line_id).where(txn_source.c.role == "additional")
    )
    assert found[0][1] == email_line


@pytest.mark.parametrize(
    "printed",
    ["qty=1.0 fee=0.0005 gross=1", "qty=0.9995 fee=0.0005"],
    ids=["gross", "net"],
)
def test_a_withdrawal_is_its_net_quantity_plus_a_fee_line(
    crypto_ledger: Ledger, printed: str
) -> None:
    ledger = crypto_ledger
    file(
        ledger,
        "withdrawal.txt",
        EXCHANGE,
        f"WITHDRAW at={NOON} asset=BTC txid=999tx0aa11 to={FOREIGN} {printed}",
    )

    ledger.derive()

    found = sorted(
        (row["movement_kind"], row["amount"], row["currency"], row["txid"],
         row["occurred_at"], row["fee_type"])
        for row in rows(ledger)
    )  # fmt: skip
    at = dt.datetime.fromisoformat(NOON)
    assert found == [
        ("crypto_fee", Decimal("-0.0005"), "BTC", "999tx0aa11", at, "withdrawal_fee"),
        ("crypto_unclassified", Decimal("-0.9995"), "BTC", "999tx0aa11", at, None),
    ]


def test_a_gross_email_and_a_net_export_of_one_withdrawal_agree_after_the_split(
    crypto_ledger: Ledger,
) -> None:
    ledger = crypto_ledger
    head = f"WITHDRAW at={NOON} asset=BTC txid=999tx0bb22 to={FOREIGN}"
    file(ledger, "email.txt", EXCHANGE, f"{head} qty=1.0 fee=0.0005 gross=1")
    file(ledger, "export.csv", EXCHANGE, f"{head} qty=0.9995 fee=0.0005")

    ledger.derive()

    assert sorted(ledger.conn.scalars(select(txn.c.amount))) == [
        Decimal("-0.9995"),
        Decimal("-0.0005"),
    ]
    for row in rows(ledger):
        assert sources(ledger, row["id"]) == ["additional", "primary"]
    assert ledger.conn.scalars(select(source_discrepancy.c.kind)).all() == []


def roles_by_amount(ledger: Ledger) -> dict[Decimal, list[str]]:
    """Each txn's amount to its sources' roles."""
    return {
        Decimal(str(row["amount"])): sources(ledger, row["id"]) for row in rows(ledger)
    }


def test_an_unnetted_primary_folds_a_lower_sources_fee_line_into_the_withdrawal(
    crypto_ledger: Ledger,
) -> None:
    # The export (a CSV, ranking first) prints one figure and no fee; the email
    # prints the same withdrawal gross with its fee. The email's fee line makes no
    # txn: the export's figure already holds the fee, and the email's net figure is a
    # quantity discrepancy, so the fee counts once.
    ledger = crypto_ledger
    head = f"WITHDRAW at={NOON} asset=BTC txid=999tx0ee55 to={FOREIGN}"
    file(ledger, "export.csv", EXCHANGE, f"{head} qty=1.0")
    file(ledger, "email.txt", EXCHANGE, f"{head} qty=1.0 fee=0.0005 gross=1")

    ledger.derive()

    assert roles_by_amount(ledger) == {
        Decimal("-1.0"): ["additional", "additional", "primary"]
    }
    assert ledger.conn.scalars(select(source_discrepancy.c.kind)).all() == [
        "quantity_mismatch"
    ]
    assert sum(ledger.conn.scalars(select(txn.c.amount))) == Decimal("-1.0")


def test_a_net_unnetted_primary_keeps_a_lower_sources_fee_as_its_own_txn(
    crypto_ledger: Ledger,
) -> None:
    # The export (ranking first) prints one figure and no fee, but that figure is
    # the net one; the email prints the withdrawal gross with its fee. The email's
    # movement plus its fee is not the export's figure, so the fee is not inside it:
    # the fee keeps its own txn and the account moves by the full gross amount.
    ledger = crypto_ledger
    head = f"WITHDRAW at={NOON} asset=BTC txid=999tx0ee66 to={FOREIGN}"
    file(ledger, "export.csv", EXCHANGE, f"{head} qty=0.9995")
    file(ledger, "email.txt", EXCHANGE, f"{head} qty=1.0 fee=0.0005 gross=1")

    ledger.derive()

    assert roles_by_amount(ledger) == {
        Decimal("-0.9995"): ["additional", "primary"],
        Decimal("-0.0005"): ["primary"],
    }
    assert sum(ledger.conn.scalars(select(txn.c.amount))) == Decimal("-1.0")
    assert ledger.conn.scalars(select(source_discrepancy.c.kind)).all() == []


def test_a_primary_printing_its_fee_on_its_own_line_keeps_the_fee_txn(
    crypto_ledger: Ledger,
) -> None:
    # A tracker export prints the net quantity and the fee as two lines with no
    # fee_stated flag; the email prints the withdrawal gross with its fee. The fee is
    # one txn with both sources, the movement another, and nothing disagrees.
    ledger = crypto_ledger
    tx = "txid=999tx0ab77"
    file(
        ledger,
        "export.csv",
        EXCHANGE,
        f"MOVE at={NOON} asset=BTC qty=-0.9995 {tx} to={FOREIGN}",
        f"FEE at={NOON} asset=BTC qty=0.0005 {tx}",
    )
    file(
        ledger,
        "email.txt",
        EXCHANGE,
        f"WITHDRAW at={NOON} asset=BTC qty=1.0 fee=0.0005 gross=1 {tx} to={FOREIGN}",
    )

    ledger.derive()

    assert roles_by_amount(ledger) == {
        Decimal("-0.9995"): ["additional", "primary"],
        Decimal("-0.0005"): ["additional", "primary"],
    }
    assert ledger.conn.scalars(select(source_discrepancy.c.kind)).all() == []


def test_an_unnetted_primary_never_folds_a_fee_in_another_asset(
    crypto_ledger: Ledger,
) -> None:
    # The export prints a token withdrawal as one figure with no fee; the email
    # prints the same TXID with its network fee in another asset. The token figure
    # cannot hold an ETH fee, so the fee stays its own txn.
    ledger = crypto_ledger
    head = f"WITHDRAW at={NOON} asset=USDT qty=50 txid=999tx0ac88 to={FOREIGN}"
    file(ledger, "export.csv", EXCHANGE, head)
    file(ledger, "email.txt", EXCHANGE, f"{head} fee=0.001 feeasset=ETH")

    ledger.derive()

    assert {
        (row["currency"], Decimal(str(row["amount"]))): sources(ledger, row["id"])
        for row in rows(ledger)
    } == {
        ("USDT", Decimal("-50")): ["additional", "primary"],
        ("ETH", Decimal("-0.001")): ["primary"],
    }
    assert ledger.conn.scalars(select(source_discrepancy.c.kind)).all() == []


@pytest.mark.parametrize(
    ("export", "email", "movement"),
    [
        ("qty=0.9995 fee=0.0005", "qty=1.0", "-0.9995"),
        ("qty=1.0 fee=0", "qty=1.0 fee=0.0005 gross=1", "-1.0"),
    ],
    ids=["net-plus-fee", "stated-zero-fee"],
)
def test_a_primary_stating_its_fee_keeps_the_rule_as_it_was(
    crypto_ledger: Ledger, export: str, email: str, movement: str
) -> None:
    # The export states its fee (a fee line, or a stated fee of zero); the email
    # ranks lower and prints another figure. The email's fee line, if any, keys as
    # before and is a txn, and the email's figure is a quantity discrepancy on the
    # withdrawal.
    ledger = crypto_ledger
    head = f"WITHDRAW at={NOON} asset=BTC txid=999tx0ff66 to={FOREIGN}"
    file(ledger, "export.csv", EXCHANGE, f"{head} {export}")
    file(ledger, "email.txt", EXCHANGE, f"{head} {email}")

    ledger.derive()

    assert roles_by_amount(ledger) == {
        Decimal(movement): ["additional", "primary"],
        Decimal("-0.0005"): ["primary"],
    }
    assert ledger.conn.scalars(select(source_discrepancy.c.kind)).all() == [
        "quantity_mismatch"
    ]


def test_a_withdrawal_with_no_stated_fee_is_one_line_carrying_its_figure(
    crypto_ledger: Ledger,
) -> None:
    ledger = crypto_ledger
    file(
        ledger,
        "withdrawal.txt",
        EXCHANGE,
        f"WITHDRAW at={NOON} asset=ETH qty=0.75 txid=999tx0cc33 to={FOREIGN}",
    )

    ledger.derive()

    [only] = rows(ledger)
    assert (only["amount"], only["movement_kind"]) == (
        Decimal("-0.75"),
        "crypto_unclassified",
    )


def test_an_unmapped_identifier_never_shares_a_real_assets_code(
    crypto_ledger: Ledger,
) -> None:
    # A token whose identifier is not listed prints the symbol of a real asset: its
    # code is its own, never USDT.
    ledger = crypto_ledger
    file(
        ledger,
        "tokens.txt",
        WALLET,
        f"MOVE at={NOON} asset=tether qty=10 type=Received",
        f"MOVE at={NOON} asset=usdt-claim-999 qty=10 type=Received",
    )

    ledger.derive()

    assert sorted(ledger.conn.scalars(select(txn.c.currency))) == [
        "USDT",
        "cs:usdt-claim-999",
    ]


# --- stage 7: exclusions --------------------------------------------------------------


def accounts_with_placeholder(ledger: Ledger) -> None:
    """The two fixture accounts and a placeholder account ``staking``."""
    columns = "monthly,,,,,,,{placeholder},,"
    ledger.manual(
        ACCOUNTS_FILE,
        *(
            f"{slug},{slug.title()},{CRYPTO_INSTITUTION},{kind},CAD,"
            + columns.format(placeholder=placeholder)
            for slug, kind, placeholder in (
                ("wallet", "crypto_wallet", ""),
                ("exchange", "exchange", ""),
                ("staking", "crypto_wallet", "true"),
            )
        ),
    )
    ledger.manual(
        IDENTIFIERS_FILE,
        f"wallet,{WALLET_ID},number,,",
        f"exchange,{EXCHANGE_ID},number,,",
        "staking,999000000083,number,,",
    )


def exclusions(ledger: Ledger) -> dict[str, str | None]:
    """Each line's exclusion, by its asset identifier."""
    found = ledger.conn.execute(
        select(txn.c.asset_identifier, txn.c.exclusion).where(txn.c.amount > 0)
    )
    return {str(asset): exclusion for asset, exclusion in found}


def test_spam_and_placeholder_lines_are_excluded_and_include_reverses_it(
    crypto_ledger: Ledger,
) -> None:
    ledger = crypto_ledger
    accounts_with_placeholder(ledger)
    file(
        ledger,
        "wallet.txt",
        WALLET,
        # Flagged fake by the source.
        f"MOVE at={NOON} asset=fake-coin-999 qty=100 type=Received fake=1",
        # Worth 0 CAD, never sent from the account: an airdrop of nothing.
        f"MOVE at={NOON} asset=airdrop-999 qty=50 type=Received cad=0.00",
        # Worth 0 CAD as stated, but the account sends this asset too.
        f"MOVE at={NOON} asset=solana qty=5 type=Received cad=0.00",
        f"MOVE at={NOON} asset=solana qty=-1 type=Sent to={FOREIGN}",
        # Worth something: an ordinary receipt.
        f"MOVE at={NOON} asset=ethereum qty=0.1 type=Received cad=250.00",
        "ACCOUNT 999000000083",
        f"MOVE at={NOON} asset=bitcoin qty=0.01 type=Received",
    )

    ledger.derive()

    assert exclusions(ledger) == {
        "fake-coin-999": "spam",
        "airdrop-999": "spam",
        "solana": None,
        "ethereum": None,
        "bitcoin": "placeholder",
    }

    by_asset = {row["asset_identifier"]: row["id"] for row in rows(ledger)}
    included = [
        record(
            ledger,
            "finance.decision.record",
            {"kind": "include", "target_ref": line_ref(by_asset[asset])},
        )
        for asset in ("fake-coin-999", "bitcoin")
    ]
    ledger.derive()

    assert exclusions(ledger)["fake-coin-999"] is None
    assert exclusions(ledger)["bitcoin"] is None
    assert exclusions(ledger)["airdrop-999"] == "spam"

    for out in included:
        assert isinstance(out, RecordOut)
        record(
            ledger, "finance.decision.reverse", {"decision_id": str(out.decision_id)}
        )
    ledger.derive()

    assert exclusions(ledger)["fake-coin-999"] == "spam"
    assert exclusions(ledger)["bitcoin"] == "placeholder"


# --- stage 7: movement kinds ----------------------------------------------------------


def test_movement_kinds_follow_the_source_the_own_addresses_and_the_trade_ref(
    crypto_ledger: Ledger,
) -> None:
    ledger = crypto_ledger
    # The address file prints the hex address upper-cased; the line prints it mixed.
    ledger.manual(OWN_ADDRESSES_FILE, f"exchange,{OWN_HEX.upper().replace('0X', '0x')}")
    file(
        ledger,
        "wallet.txt",
        WALLET,
        f"MOVE at={NOON} asset=ethereum qty=-0.5 type=Sent to={OWN_HEX}",
        f"MOVE at={NOON} asset=solana qty=3 type=Received hint=1",
        f"MOVE at={NOON} asset=bitcoin qty=-0.02 type=Trade trade=T999A",
        f"MOVE at={NOON} asset=ethereum qty=0.6 type=Trade trade=T999A",
        f"MOVE at={NOON} asset=tether qty=40 type=Buy",
        f"MOVE at={NOON} asset=usd-coin qty=-5 type=Sent to={FOREIGN}",
        f"FEE at={NOON} asset=ethereum qty=0.001 type=Fee",
        f"MOVE at={NOON} asset=fake-coin-999 qty=9 type=Received hint=1 fake=1",
    )

    ledger.derive()

    found = {
        (row["asset_identifier"], row["amount"]): (
            row["movement_kind"], row["transfer_like"], row["transfer_state"]
        )
        for row in rows(ledger)
    }  # fmt: skip
    assert found == {
        # To an own address with no receiving row loaded: a one-sided own transfer.
        ("ethereum", Decimal("-0.5")): ("crypto_transfer", True, "paired"),
        ("solana", Decimal("3")): ("crypto_transfer", True, "unresolved"),
        ("bitcoin", Decimal("-0.02")): ("crypto_swap_leg", False, None),
        ("ethereum", Decimal("0.6")): ("crypto_swap_leg", False, None),
        ("tether", Decimal("40")): ("crypto_unclassified", False, None),
        ("usd-coin", Decimal("-5")): ("crypto_unclassified", False, None),
        ("ethereum", Decimal("-0.001")): ("crypto_fee", False, None),
        # Spam keeps its kind and is never transfer-like.
        ("fake-coin-999", Decimal("9")): ("crypto_transfer", False, None),
    }


def test_a_trade_ref_with_one_sign_is_no_swap(crypto_ledger: Ledger) -> None:
    # Two legs of one trade ref with both signs are a swap; a trade ref whose lines
    # all leave the account is not, and stays unclassified.
    ledger = crypto_ledger
    file(
        ledger,
        "wallet.txt",
        WALLET,
        f"MOVE at={NOON} asset=bitcoin qty=-0.02 type=Trade trade=T999A",
        f"MOVE at={NOON} asset=ethereum qty=0.6 type=Trade trade=T999A",
        f"MOVE at={NOON} asset=solana qty=-2 type=Trade trade=T999B",
        f"MOVE at={NOON} asset=cardano qty=-7 type=Trade trade=T999B",
    )

    ledger.derive()

    assert {row["asset_identifier"]: row["movement_kind"] for row in rows(ledger)} == {
        "bitcoin": "crypto_swap_leg",
        "ethereum": "crypto_swap_leg",
        "solana": "crypto_unclassified",
        "cardano": "crypto_unclassified",
    }


def test_a_fee_and_a_movement_of_one_figure_and_second_stay_two_txns(
    crypto_ledger: Ledger,
) -> None:
    # Two documents print the same fee and the same movement (no TXID, one figure,
    # one second, one source type), in opposite orders. The line kind keeps them two
    # txns, each with its two sources of its own kind.
    ledger = crypto_ledger
    move = f"MOVE at={NOON} asset=ethereum qty=-0.001 type=Sent"
    fee = f"FEE at={NOON} asset=ethereum qty=0.001 type=Sent"
    file(ledger, "first.csv", WALLET, move, fee)
    file(ledger, "second.txt", WALLET, fee, move)

    ledger.derive()

    kinds = ledger.conn.execute(
        select(txn.c.movement_kind, document_line.c.line_kind)
        .join(txn_source, txn_source.c.txn_id == txn.c.id)
        .join(document_line, document_line.c.id == txn_source.c.document_line_id)
    ).all()
    assert sorted(tuple(row) for row in kinds) == [
        ("crypto_fee", "crypto_fee"),
        ("crypto_fee", "crypto_fee"),
        ("crypto_unclassified", "crypto_movement"),
        ("crypto_unclassified", "crypto_movement"),
    ]


# --- the fixture parser never ships ---------------------------------------------------


def test_the_crypto_fixture_parser_is_registered_only_inside_a_test() -> None:
    assert not any(
        parser_id.startswith(CRYPTO_PARSER_PREFIX)
        for parser_id in registry.load_parsers()
    )
    assert CRYPTO_ISSUER not in issuers.ISSUER_DIRS
    named = [
        path.relative_to(SRC).as_posix()
        for path in SRC.rglob("*")
        if path.is_file()
        and path.suffix in (".py", ".json", ".toml")
        and any(
            text in path.read_text(encoding="utf-8")
            for text in (CRYPTO_ISSUER, CRYPTO_PARSER_PREFIX, CRYPTO_HEADER)
        )
    ]
    assert named == []


def test_the_crypto_line_kinds_are_line_kinds() -> None:
    # Named once in parsers.base; the line kind type must hold both.
    assert set(CRYPTO_LINE_KINDS) <= set(typing.get_args(LineKind))
    assert len(set(CRYPTO_LINE_KINDS)) == 2
