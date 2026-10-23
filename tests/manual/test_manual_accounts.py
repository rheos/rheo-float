"""The account registry load (FR-1, FR-10, AC-1, the bad-row half of AC-10).

Every test goes through ``manual.stage2.load_accounts`` with CSV files written to a
temporary data directory, and reads the result back from the tables, inside the
test's rolled-back transaction. Every value is synthetic.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest
from sqlalchemy import Connection, insert, select

from rheo_finance.context import UnitOfWork
from rheo_finance.manual.accounts import ACCOUNTS_FILE, IDENTIFIERS_FILE
from rheo_finance.manual.headers import MANUAL_FILES
from rheo_finance.manual.stage2 import load_accounts
from rheo_finance.storage.schema import account, account_identifier, manual_entry_error

ACCOUNTS_HEADER = ",".join(MANUAL_FILES[ACCOUNTS_FILE])
IDENTIFIERS_HEADER = ",".join(MANUAL_FILES[IDENTIFIERS_FILE])

CHEQUING = "test-chequing,Test chequing,Test Credit Union,chequing,CAD,monthly,"
CHEQUING += "self,2015-01-01,,documents,,,,,"
CARD = "test-card,Test card,Example Bank,credit_card,CAD;usd,monthly,self,,,,,,,,"
SAVINGS = "test-savings,Test savings,Test Credit Union,savings,CAD,monthly,,,,,,,,,"


def write(data: Path, accounts: list[str], identifiers: list[str]) -> None:
    manual = data / "manual"
    manual.mkdir(exist_ok=True)
    (manual / ACCOUNTS_FILE).write_text(
        "\n".join([ACCOUNTS_HEADER, *accounts]) + "\n", encoding="utf-8"
    )
    (manual / IDENTIFIERS_FILE).write_text(
        "\n".join([IDENTIFIERS_HEADER, *identifiers]) + "\n", encoding="utf-8"
    )


def slugs(conn: Connection) -> list[str]:
    return sorted(conn.scalars(select(account.c.slug)))


def errors(conn: Connection) -> list[tuple[str, int, str]]:
    rows = conn.execute(
        select(
            manual_entry_error.c.file,
            manual_entry_error.c.line_no,
            manual_entry_error.c.reason,
        )
    )
    return sorted((str(f), int(n), str(r)) for f, n, r in rows)


def error_lines(conn: Connection) -> list[tuple[str, int]]:
    return sorted({(file, line) for file, line, _ in errors(conn)})


def test_a_good_registry_loads_with_the_built_in_account(
    owner_conn: Connection, tmp_path: Path
) -> None:
    write(
        tmp_path,
        [CHEQUING, CARD],
        [
            "test-chequing,9991234,number,,",
            "test-card,9999 99** **** 4321,masked_card,2020-01-01,2023-12-31",
            "test-card,9999 99** **** 8765,masked_card,2024-01-01,",
        ],
    )
    registry = load_accounts(UnitOfWork(owner_conn), tmp_path)

    assert registry.errors == ()
    assert errors(owner_conn) == []
    assert slugs(owner_conn) == ["paid-outside", "test-card", "test-chequing"]
    card = owner_conn.execute(
        select(account.c.currencies, account.c.cadence, account.c.sourced_by).where(
            account.c.slug == "test-card"
        )
    ).one()
    assert tuple(card) == (["CAD", "USD"], "monthly", "documents")
    assert owner_conn.scalar(select(account_identifier.c.id).limit(1)) is not None
    assert len(list(owner_conn.scalars(select(account_identifier.c.id)))) == 4


def test_the_built_in_account_exists_with_no_manual_files(
    owner_conn: Connection, tmp_path: Path
) -> None:
    registry = load_accounts(UnitOfWork(owner_conn), tmp_path)

    assert registry.errors == ()
    row = owner_conn.execute(
        select(account.c.id, account.c.label, account.c.kind).where(
            account.c.slug == "paid-outside"
        )
    ).one()
    assert (row.label, row.kind) == ("Paid outside recorded accounts", "wallet")
    ident = owner_conn.execute(
        select(account_identifier.c.identifier, account_identifier.c.kind).where(
            account_identifier.c.account_id == row.id
        )
    ).one()
    assert tuple(ident) == ("CASH", "internal")


def test_one_bad_row_reports_its_line_and_every_other_row_loads(
    owner_conn: Connection, tmp_path: Path
) -> None:
    bad = "test-bad,Test bad,Example Bank,brokerage,CAD,monthly,,,,,,,,,"
    write(
        tmp_path,
        [CHEQUING, bad, CARD],
        ["test-chequing,9991234,number,,", "test-card,9995678,number,,"],
    )
    load_accounts(UnitOfWork(owner_conn), tmp_path)

    assert error_lines(owner_conn) == [(ACCOUNTS_FILE, 3)]
    assert errors(owner_conn)[0][2].startswith("kind: ")
    assert slugs(owner_conn) == ["paid-outside", "test-card", "test-chequing"]


def test_a_bad_identifier_row_reports_its_line_and_others_load(
    owner_conn: Connection, tmp_path: Path
) -> None:
    write(
        tmp_path,
        [CHEQUING, CARD],
        [
            "test-chequing,9991234,number,,",
            "test-card,9995678,number,2024-02-01,2024-01-01",
            "test-card,9998765,number,,",
        ],
    )
    load_accounts(UnitOfWork(owner_conn), tmp_path)

    assert error_lines(owner_conn) == [(IDENTIFIERS_FILE, 3)]
    assert slugs(owner_conn) == ["paid-outside", "test-card", "test-chequing"]


def test_adding_an_account_is_data_entry_alone(
    owner_conn: Connection, tmp_path: Path
) -> None:
    uow = UnitOfWork(owner_conn)
    write(tmp_path, [CHEQUING], ["test-chequing,9991234,number,,"])
    load_accounts(uow, tmp_path)
    assert slugs(owner_conn) == ["paid-outside", "test-chequing"]

    write(
        tmp_path,
        [CHEQUING, SAVINGS],
        ["test-chequing,9991234,number,,", "test-savings,9995678,number,,"],
    )
    load_accounts(uow, tmp_path)

    assert slugs(owner_conn) == ["paid-outside", "test-chequing", "test-savings"]
    assert errors(owner_conn) == []


def test_an_account_with_no_identifier_is_rejected(
    owner_conn: Connection, tmp_path: Path
) -> None:
    write(tmp_path, [CHEQUING, SAVINGS], ["test-chequing,9991234,number,,"])
    load_accounts(UnitOfWork(owner_conn), tmp_path)

    assert errors(owner_conn) == [
        (ACCOUNTS_FILE, 3, "the account has no valid identifier row")
    ]
    assert slugs(owner_conn) == ["paid-outside", "test-chequing"]


def test_a_shared_last_four_at_one_institution_is_an_error_on_both_rows(
    owner_conn: Connection, tmp_path: Path
) -> None:
    write(
        tmp_path,
        [CHEQUING, SAVINGS, CARD],
        [
            # A last-four-only number beside a full one: a last-four match could
            # confuse them.
            "test-chequing,XXXX1234,number,,",
            "test-savings,999-555-1234,number,,",
            # Another institution, and a second number of the same account: no clash.
            "test-card,9997771234,number,,",
            "test-card,9996661234,number,,",
        ],
    )
    registry = load_accounts(UnitOfWork(owner_conn), tmp_path)

    shared = [
        (file, line)
        for file, line, reason in errors(owner_conn)
        if "last four" in reason
    ]
    assert shared == [(IDENTIFIERS_FILE, 2), (IDENTIFIERS_FILE, 3)]
    # Both accounts had only the clashing number, so neither has one left.
    assert error_lines(owner_conn) == sorted(
        [
            (ACCOUNTS_FILE, 2),
            (ACCOUNTS_FILE, 3),
            (IDENTIFIERS_FILE, 2),
            (IDENTIFIERS_FILE, 3),
        ]
    )
    assert slugs(owner_conn) == ["paid-outside", "test-card"]
    assert [row.slug for row in registry.accounts] == ["paid-outside", "test-card"]


# The same institution as CHEQUING, spelled with other case and spacing.
VISA = "test-visa,Test visa,test  credit   UNION,credit_card,CAD,monthly,,,,,,,,,"
# Punctuation is not normalized: another institution.
OTHER = "test-other,Test other,Test Credit Union.,savings,CAD,monthly,,,,,,,,,"


@pytest.mark.parametrize(
    ("identifiers", "clashing"),
    [
        # A number and a masked card of two accounts at one institution.
        (
            [
                "test-chequing,9991234,number,,",
                "test-visa,9999 99** **** 1234,masked_card,,",
            ],
            [2, 3],
        ),
        # Two masked cards.
        (
            [
                "test-chequing,9999 88** **** 1234,masked_card,,",
                "test-visa,9999 99** **** 1234,masked_card,,",
            ],
            [2, 3],
        ),
        # A number of four digits beside a full number.
        (["test-chequing,1234,number,,", "test-visa,9995551234,number,,"], [2, 3]),
        # A masked number (more than four digits shown) beside a full number.
        (
            [
                "test-chequing,9999 99** **** 1234,number,,",
                "test-visa,9995551234,number,,",
            ],
            [2, 3],
        ),
        # A single X inside a digit group masks the digits it stands for.
        (
            [
                "test-chequing,9999 99X 9991234,number,,",
                "test-visa,9995551234,number,,",
            ],
            [2, 3],
        ),
        # X as the only letter, standing apart, is a mask too.
        (
            ["test-chequing,9999 X 9991234,number,,", "test-visa,9995551234,number,,"],
            [2, 3],
        ),
        # An account ID with an X beside a digit reads as masked: refused, the safe way.
        (
            ["test-chequing,SAMPLEX9991234,number,,", "test-visa,9995551234,number,,"],
            [2, 3],
        ),
        # An X after a digit in an account ID reads as masked too.
        (
            ["test-chequing,AB999X 9991234,number,,", "test-visa,9995551234,number,,"],
            [2, 3],
        ),
        # An X between other letters of an account ID is a letter: both load.
        (
            ["test-chequing,SAXMPLE9991234,number,,", "test-visa,9995551234,number,,"],
            [],
        ),
        # One full number on two accounts, written with other separators.
        (["test-chequing,9991234,number,,", "test-visa,999-1234,number,,"], [2, 3]),
        # Two distinct full numbers: a parser that matches the whole number tells
        # them apart, so they may end alike.
        (["test-chequing,9991234,number,,", "test-visa,9995551234,number,,"], []),
        # A file token is not matched by last four, so it never clashes.
        (["test-chequing,9991234,number,,", "test-visa,stmt-1234,file_token,,"], []),
        # Punctuation makes another institution.
        (["test-chequing,9991234,number,,", "test-other,9995551234,number,,"], []),
    ],
)
def test_the_last_four_rule_covers_numbers_and_masked_cards(
    owner_conn: Connection,
    tmp_path: Path,
    identifiers: list[str],
    clashing: list[int],
) -> None:
    write(tmp_path, [CHEQUING, VISA, OTHER], identifiers)
    load_accounts(UnitOfWork(owner_conn), tmp_path)

    shared = [
        line
        for file, line, reason in errors(owner_conn)
        if file == IDENTIFIERS_FILE and "last four" in reason
    ]
    assert shared == clashing


@pytest.mark.parametrize(
    ("accounts", "identifiers", "expected"),
    [
        # Two account rows with one slug: both rejected.
        (
            [CHEQUING, CHEQUING],
            ["test-chequing,9991234,number,,"],
            [(ACCOUNTS_FILE, 2), (ACCOUNTS_FILE, 3), (IDENTIFIERS_FILE, 2)],
        ),
        # The built-in account's slug is reserved.
        (
            ["paid-outside,Mine,Example Bank,wallet,CAD,none,,,,,,,,,"],
            ["paid-outside,9991234,number,,"],
            [(ACCOUNTS_FILE, 2), (IDENTIFIERS_FILE, 2)],
        ),
        # An identifier naming no account, and the reserved kind.
        (
            [CHEQUING],
            [
                "test-chequing,9991234,number,,",
                "test-missing,9995678,number,,",
                "test-chequing,CASH2,internal,,",
            ],
            [(IDENTIFIERS_FILE, 3), (IDENTIFIERS_FILE, 4)],
        ),
        # One identifier on two rows: both rejected.
        (
            [CHEQUING, CARD],
            [
                "test-chequing,9991234,number,,",
                "test-card,9991234,number,,",
                "test-card,9995678,number,,",
            ],
            [(IDENTIFIERS_FILE, 2), (IDENTIFIERS_FILE, 3), (ACCOUNTS_FILE, 2)],
        ),
    ],
)
def test_the_rules_across_rows_reject_every_row_involved(
    owner_conn: Connection,
    tmp_path: Path,
    accounts: list[str],
    identifiers: list[str],
    expected: list[tuple[str, int]],
) -> None:
    write(tmp_path, accounts, identifiers)
    load_accounts(UnitOfWork(owner_conn), tmp_path)

    assert error_lines(owner_conn) == sorted(expected)


def test_a_reload_replaces_its_rows_and_keeps_other_files_errors(
    owner_conn: Connection, tmp_path: Path
) -> None:
    owner_conn.execute(
        insert(manual_entry_error).values(
            id=uuid.uuid4(), file="transactions.csv", line_no=4, reason="planted"
        )
    )
    write(tmp_path, [CHEQUING, SAVINGS], ["test-chequing,9991234,number,,"])
    uow = UnitOfWork(owner_conn)
    load_accounts(uow, tmp_path)
    first_ids = sorted(owner_conn.scalars(select(account.c.id)))
    first_identifier_ids = sorted(owner_conn.scalars(select(account_identifier.c.id)))
    load_accounts(uow, tmp_path)

    assert error_lines(owner_conn) == [(ACCOUNTS_FILE, 3), ("transactions.csv", 4)]
    # Derived ids: a rebuild reproduces every one.
    assert sorted(owner_conn.scalars(select(account.c.id))) == first_ids
    assert (
        sorted(owner_conn.scalars(select(account_identifier.c.id)))
        == first_identifier_ids
    )


def test_a_bad_header_is_one_file_level_error(
    owner_conn: Connection, tmp_path: Path
) -> None:
    write(tmp_path, [CHEQUING], ["test-chequing,9991234,number,,"])
    path = tmp_path / "manual" / IDENTIFIERS_FILE
    path.write_text(
        "account,identifier,kind,valid_from\ntest-chequing,9991234,number,\n",
        encoding="utf-8",
    )
    load_accounts(UnitOfWork(owner_conn), tmp_path)

    assert error_lines(owner_conn) == sorted(
        [(ACCOUNTS_FILE, 2), (IDENTIFIERS_FILE, 1)]
    )
    assert slugs(owner_conn) == ["paid-outside"]


def test_no_reason_repeats_an_identifier(
    owner_conn: Connection, tmp_path: Path
) -> None:
    write(
        tmp_path,
        [CHEQUING, SAVINGS],
        [
            "test-chequing,9991234,cardnumber,,",
            # A last-four-only number beside a full one ending alike: a clash.
            "test-chequing,XXXX5678,number,,",
            "test-savings,9998885678,number,,",
        ],
    )
    load_accounts(UnitOfWork(owner_conn), tmp_path)

    reasons = [reason for _, _, reason in errors(owner_conn)]
    assert len(reasons) == 5
    assert not any("999" in reason or "678" in reason for reason in reasons)


# --- slugs and labels carry no identifier (the report views show both) ---------------


@pytest.mark.parametrize(
    ("row", "field"),
    [
        # A slug that is a digit run.
        ("99912345678,Test digits,Example Bank,savings,CAD,monthly,,,,,,,,,", "slug"),
        # A label carrying the registered identifier with separators.
        (
            "test-savings,Savings 9990 0001 234,Example Bank,savings,CAD,monthly"
            + ",,,,,,,,,",
            "label",
        ),
        # A label carrying a long digit run that is not a date.
        (
            "test-savings,Savings 99900012345,Example Bank,savings,CAD,monthly"
            + ",,,,,,,,,",
            "label",
        ),
    ],
)
def test_a_slug_or_label_carrying_an_identifier_is_a_manual_entry_error(
    owner_conn: Connection, tmp_path: Path, row: str, field: str
) -> None:
    slug = row.split(",", 1)[0]
    write(
        tmp_path,
        [CHEQUING, row],
        ["test-chequing,99900001234,number,,", f"{slug},9995678,number,,"],
    )
    load_accounts(UnitOfWork(owner_conn), tmp_path)

    found = [entry for entry in errors(owner_conn) if entry[:2] == (ACCOUNTS_FILE, 3)]
    assert [reason.split(":")[0] for _, _, reason in found] == [field]
    assert not any("999" in reason for _, _, reason in errors(owner_conn))
    assert slugs(owner_conn) == ["paid-outside", "test-chequing"]


def test_a_label_with_a_date_or_a_short_number_still_loads(
    owner_conn: Connection, tmp_path: Path
) -> None:
    label = "test-savings,Savings 2024 20240131 no 12,Example Bank,savings,CAD,monthly,"
    write(
        tmp_path,
        [CHEQUING, label + ",,,,,,,,"],
        ["test-chequing,99900001234,number,,", "test-savings,9995678,number,,"],
    )
    load_accounts(UnitOfWork(owner_conn), tmp_path)

    assert errors(owner_conn) == []
    assert slugs(owner_conn) == ["paid-outside", "test-chequing", "test-savings"]
