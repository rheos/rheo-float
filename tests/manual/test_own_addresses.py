"""``own_addresses.csv`` through the public stage 2 loader (FR-10, FR-15(b); AC-10 bad
rows): rows load into ``own_address`` in their compared form, and a bad row is
reported by its line number while every other row loads. Every address is invented.
"""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import Connection, select

from rheo_finance.context import UnitOfWork
from rheo_finance.manual.accounts import ACCOUNTS_FILE, IDENTIFIERS_FILE
from rheo_finance.manual.headers import MANUAL_FILES
from rheo_finance.manual.own_addresses import OWN_ADDRESSES_FILE, OwnAddressRow
from rheo_finance.manual.stage2 import account_id, load_accounts, load_own_addresses
from rheo_finance.storage.schema import manual_entry_error, own_address


def write(data: Path, name: str, rows: list[str]) -> None:
    manual = data / "manual"
    manual.mkdir(exist_ok=True)
    header = ",".join(MANUAL_FILES[name])
    (manual / name).write_text("\n".join([header, *rows]) + "\n", encoding="utf-8")


def registry(data: Path) -> None:
    write(
        data,
        ACCOUNTS_FILE,
        [
            "wallet,Wallet,Sample Crypto,crypto_wallet,CAD,none,,,,,,,,,",
            "exchange,Exchange,Sample Crypto,exchange,CAD,none,,,,,,,,,",
        ],
    )
    write(
        data,
        IDENTIFIERS_FILE,
        ["wallet,999000000081,number,,", "exchange,999000000082,number,,"],
    )


def stored(conn: Connection) -> dict[str, object]:
    return {
        address: account
        for address, account in conn.execute(
            select(own_address.c.address, own_address.c.account_id)
        )
    }


def errors(conn: Connection) -> dict[int, str]:
    rows = conn.execute(
        select(manual_entry_error.c.line_no, manual_entry_error.c.reason).where(
            manual_entry_error.c.file == OWN_ADDRESSES_FILE
        )
    )
    return {int(line): str(reason) for line, reason in rows}


def test_the_row_model_has_the_files_columns() -> None:
    assert tuple(OwnAddressRow.model_fields) == MANUAL_FILES[OWN_ADDRESSES_FILE]


def test_hex_addresses_are_lower_cased_and_others_kept_as_printed(
    owner_conn: Connection, tmp_path: Path
) -> None:
    uow = UnitOfWork(owner_conn)
    registry(tmp_path)
    write(
        tmp_path,
        OWN_ADDRESSES_FILE,
        [
            "wallet,0xAbCd999Ef01",
            "exchange,Sample9Base58Address",
            # The same hex address again, in another case, for the same account.
            "wallet,0XABCD999EF01",
        ],
    )

    found = load_own_addresses(uow, tmp_path, load_accounts(uow, tmp_path))

    assert stored(owner_conn) == {
        "0xabcd999ef01": account_id("wallet"),
        "Sample9Base58Address": account_id("exchange"),
    }
    assert found.errors == ()


def test_a_bad_row_is_reported_and_every_other_row_loads(
    owner_conn: Connection, tmp_path: Path
) -> None:
    uow = UnitOfWork(owner_conn)
    registry(tmp_path)
    write(
        tmp_path,
        OWN_ADDRESSES_FILE,
        [
            "wallet,Sample9Kept",
            "nowhere,Sample9Unknown",
            "wallet,0x999aa",
            "exchange,0x999AA",
            "wallet,with space",
            "exchange,",
        ],
    )

    load_own_addresses(uow, tmp_path, load_accounts(uow, tmp_path))

    assert stored(owner_conn) == {"Sample9Kept": account_id("wallet")}
    reasons = errors(owner_conn)
    assert sorted(reasons) == [3, 4, 5, 6, 7]
    assert reasons[3] == "account: no loaded account has this slug"
    assert reasons[4] == reasons[5] == "address: another row gives it another account"
    assert "whitespace" in reasons[6]
    assert reasons[7].startswith("address")
