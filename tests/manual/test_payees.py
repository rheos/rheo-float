"""``payees.csv`` and the account-backed and bill-issuer payees, through the public
stage 2 loader (FR-10, FR-46; AC-10 payee clauses): rows load into ``payee`` and are
held in memory with their ``paid_from``; a label or slug equal to one of the row's own
patterns, and every other bad row, is reported by its line number while every other
row loads. Every name and number is invented.
"""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import Connection, select

from rheo_finance.context import UnitOfWork
from rheo_finance.manual.accounts import ACCOUNTS_FILE, IDENTIFIERS_FILE
from rheo_finance.manual.headers import MANUAL_FILES
from rheo_finance.manual.payees import PAYEES_FILE, PayeeRow, Payees
from rheo_finance.manual.stage2 import (
    account_id,
    load_accounts,
    load_payees,
    payee_id,
)
from rheo_finance.storage.schema import manual_entry_error, payee

CARD_PATTERNS = "SAMPLE CARD PAYMENT;SAMPLE CARD PMT"


def write(data: Path, name: str, rows: list[str]) -> None:
    manual = data / "manual"
    manual.mkdir(exist_ok=True)
    header = ",".join(MANUAL_FILES[name])
    (manual / name).write_text("\n".join([header, *rows]) + "\n", encoding="utf-8")


def registry(data: Path, card_patterns: str = CARD_PATTERNS) -> None:
    write(
        data,
        ACCOUNTS_FILE,
        [
            "chequing,Chequing,Sample Bank,chequing,CAD,monthly,,,,,,,,,",
            f"card,Card,Sample Bank,credit_card,CAD,monthly,,,,,,{card_patterns},,,",
        ],
    )
    write(
        data,
        IDENTIFIERS_FILE,
        ["chequing,999000000091,number,,", "card,9999 99XX XXXX 9992,masked_card,,"],
    )


def load(conn: Connection, data: Path, rows: list[str]) -> Payees:
    uow = UnitOfWork(conn)
    write(data, PAYEES_FILE, rows)
    return load_payees(uow, data, load_accounts(uow, data))


def stored(conn: Connection) -> dict[str, tuple[object, ...]]:
    rows = conn.execute(
        select(
            payee.c.id,
            payee.c.slug,
            payee.c.display_label,
            payee.c.kind,
            payee.c.account_id,
            payee.c.party_ref,
            payee.c.patterns,
        )
    )
    return {
        row.slug: (
            row.id, row.display_label, row.kind, row.account_id, row.party_ref,
            tuple(row.patterns),
        )
        for row in rows
    }  # fmt: skip


def errors(conn: Connection) -> dict[int, str]:
    rows = conn.execute(
        select(manual_entry_error.c.line_no, manual_entry_error.c.reason).where(
            manual_entry_error.c.file == PAYEES_FILE
        )
    )
    return {int(line): str(reason) for line, reason in rows}


def test_the_row_model_has_the_files_columns() -> None:
    assert tuple(PayeeRow.model_fields) == MANUAL_FILES[PAYEES_FILE]


def test_rows_load_with_their_patterns_party_and_paid_from(
    owner_conn: Connection, tmp_path: Path
) -> None:
    registry(tmp_path)
    found = load(
        owner_conn,
        tmp_path,
        [
            "landlord,Landlord,person,SAMPLE LANDLORD;S. Landlord,sample-party,"
            "chequing",
            "sample-isp,Sample Internet,vendor,SAMPLE ISP 999,,",
        ],
    )

    assert errors(owner_conn) == {}
    rows = stored(owner_conn)
    assert rows["landlord"] == (
        payee_id("landlord"), "Landlord", "person", None, "sample-party",
        ("sample landlord", "s. landlord"),
    )  # fmt: skip
    assert rows["sample-isp"][1:] == (
        "Sample Internet", "vendor", None, None, ("sample isp 999",),
    )  # fmt: skip
    held = {each.slug: each for each in found.payees}
    assert held["landlord"].paid_from == ("chequing",)
    assert held["landlord"].line_no == 2


def test_a_row_without_paid_from_loads_unchanged(
    owner_conn: Connection, tmp_path: Path
) -> None:
    """No ``paid_from``: the row loads as it would with the column absent, and holds
    no paying accounts, so stage 14 keeps the derived ones."""
    registry(tmp_path)
    found = load(owner_conn, tmp_path, ["landlord,Landlord,person,SAMPLE LANDLORD,,"])

    assert errors(owner_conn) == {}
    (listed,) = [each for each in found.payees if each.line_no is not None]
    assert (listed.slug, listed.label, listed.kind, listed.patterns) == (
        "landlord", "Landlord", "person", ("sample landlord",),
    )  # fmt: skip
    assert listed.paid_from == ()
    assert listed.party is None


def test_a_label_or_slug_equal_to_its_own_pattern_is_rejected(
    owner_conn: Connection, tmp_path: Path
) -> None:
    """A printed name never becomes a label by accident (FR-46): compared without
    case, punctuation or spacing, a label or slug equal to one of the row's own
    patterns rejects that row, and every other row loads."""
    registry(tmp_path)
    found = load(
        owner_conn,
        tmp_path,
        [
            "landlord,Sample Landlord,person,SAMPLE  LANDLORD,,",
            "sample-isp,Internet,vendor,Sample ISP,,",
            "plumber,Plumber,vendor,SAMPLE PLUMBING,,",
        ],
    )

    reasons = errors(owner_conn)
    assert sorted(reasons) == [2, 3]
    assert "label: equals one of the row's own patterns" in reasons[2]
    assert "slug: equals one of the row's own patterns" in reasons[3]
    assert "SAMPLE" not in reasons[2] + reasons[3]  # a reason never holds the cell
    assert "plumber" in stored(owner_conn)
    assert {"landlord", "sample-isp"}.isdisjoint(stored(owner_conn))
    assert {each.slug for each in found.payees if each.line_no} == {"plumber"}


def test_a_label_that_only_contains_a_pattern_loads(
    owner_conn: Connection, tmp_path: Path
) -> None:
    registry(tmp_path)
    load(owner_conn, tmp_path, ["isp,Sample ISP at home,vendor,SAMPLE ISP,,"])

    assert errors(owner_conn) == {}
    assert "isp" in stored(owner_conn)


def test_account_backed_payees_come_from_the_accounts_payee_patterns(
    owner_conn: Connection, tmp_path: Path
) -> None:
    registry(tmp_path)
    load(owner_conn, tmp_path, [])

    rows = stored(owner_conn)
    assert rows["card"][1:] == (
        "Card", "account", account_id("card"), None,
        ("sample card payment", "sample card pmt"),
    )  # fmt: skip
    assert "chequing" not in rows  # no payee_patterns, no payee


def test_bill_issuers_are_vendor_payees_unless_a_row_names_them(
    owner_conn: Connection, tmp_path: Path
) -> None:
    registry(tmp_path)
    load(owner_conn, tmp_path, ["fortisbc,Gas and power,vendor,SAMPLE GAS CO,,"])

    rows = stored(owner_conn)
    assert rows["virgin-plus"][1:] == ("virgin-plus", "vendor", None, None, ())
    assert rows["fortisbc"][1:] == (
        "Gas and power", "vendor", None, None, ("sample gas co",),
    )  # fmt: skip


def test_bad_rows_are_reported_by_line_and_the_rest_load(
    owner_conn: Connection, tmp_path: Path
) -> None:
    registry(tmp_path)
    load(
        owner_conn,
        tmp_path,
        [
            "landlord,Landlord,person,SAMPLE LANDLORD,,",  # 2: loads
            "twice,Twice,vendor,SAMPLE ONE,,",  # 3: slug repeated
            "twice,Twice again,vendor,SAMPLE TWO,,",  # 4: slug repeated
            "card,My card,vendor,SAMPLE CARD,,",  # 5: the account-backed payee's slug
            "savings,Savings,account,SAMPLE SAVINGS,,",  # 6: no such account
            "gym,Gym,vendor,SAMPLE GYM,,missing",  # 7: paid_from unknown
            "shop,Shop 99912345,vendor,SAMPLE SHOP,,",  # 8: label carries 8 digits
            "chequing,Chequing,account,SAMPLE OWN,,",  # 9: loads, account-backed
            "club,Club,friend,SAMPLE CLUB,,",  # 10: kind unknown
        ],
    )

    reasons = errors(owner_conn)
    assert sorted(reasons) == [3, 4, 5, 6, 7, 8, 10]
    assert reasons[5].startswith("slug: the account-backed payee")
    assert reasons[6].startswith("slug: a payee of kind account")
    assert reasons[7].startswith("paid_from:")
    assert reasons[8].startswith("label: carries an account identifier")
    assert reasons[10].startswith("kind:")
    rows = stored(owner_conn)
    assert {"landlord", "chequing"} <= set(rows)
    assert rows["chequing"][2:4] == ("account", account_id("chequing"))
    assert {"twice", "savings", "gym", "shop", "club"}.isdisjoint(rows)
    assert rows["card"][2] == "account"


def test_a_load_rewrites_the_table_and_its_own_errors_only(
    owner_conn: Connection, tmp_path: Path
) -> None:
    registry(tmp_path)
    load(owner_conn, tmp_path, ["bad,Bad,nobody,SAMPLE BAD,,"])
    assert sorted(errors(owner_conn)) == [2]

    load(owner_conn, tmp_path, ["landlord,Landlord,person,SAMPLE LANDLORD,,"])

    assert errors(owner_conn) == {}
    assert "landlord" in stored(owner_conn)
    assert "bad" not in stored(owner_conn)
    other = owner_conn.execute(
        select(manual_entry_error.c.file).where(
            manual_entry_error.c.file != PAYEES_FILE
        )
    ).all()
    assert other == []
