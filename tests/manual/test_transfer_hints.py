"""``transfer_hints.csv`` through the public stage 2 loader (FR-10, FR-15; AC-10 bad
rows): rows load in file order with their patterns normalized, and a bad row is
reported by its line number while every other row loads. Nothing is stored but the
errors. Every value is synthetic.
"""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import Connection, select

from rheo_finance.context import UnitOfWork
from rheo_finance.manual.accounts import ACCOUNTS_FILE, IDENTIFIERS_FILE
from rheo_finance.manual.headers import MANUAL_FILES
from rheo_finance.manual.stage2 import load_accounts, load_transfer_hints
from rheo_finance.manual.transfer_hints import TRANSFER_HINTS_FILE, TransferHintRow
from rheo_finance.storage.schema import manual_entry_error


def write(data: Path, name: str, rows: list[str]) -> None:
    manual = data / "manual"
    manual.mkdir(exist_ok=True)
    header = ",".join(MANUAL_FILES[name])
    (manual / name).write_text("\n".join([header, *rows]) + "\n", encoding="utf-8")


def stored_errors(conn: Connection) -> dict[int, str]:
    rows = conn.execute(
        select(manual_entry_error.c.line_no, manual_entry_error.c.reason).where(
            manual_entry_error.c.file == TRANSFER_HINTS_FILE
        )
    )
    return {int(line): str(reason) for line, reason in rows}


def test_the_row_model_has_the_files_columns() -> None:
    assert tuple(TransferHintRow.model_fields) == MANUAL_FILES[TRANSFER_HINTS_FILE]


def test_a_missing_or_header_only_file_loads_as_empty(
    owner_conn: Connection, tmp_path: Path
) -> None:
    uow = UnitOfWork(owner_conn)
    registry = load_accounts(uow, tmp_path)

    missing = load_transfer_hints(uow, tmp_path, registry)
    write(tmp_path, TRANSFER_HINTS_FILE, [])
    empty = load_transfer_hints(uow, tmp_path, registry)

    assert (missing.hints, missing.errors) == ((), ())
    assert (empty.hints, empty.errors) == ((), ())


def test_rows_load_in_order_and_bad_rows_are_reported_by_line(
    owner_conn: Connection, tmp_path: Path
) -> None:
    write(
        tmp_path,
        ACCOUNTS_FILE,
        ["test-savings,Test savings,Test Bank,savings,CAD,none,,,,,,,,,"],
    )
    write(tmp_path, IDENTIFIERS_FILE, ["test-savings,9991,number,,"])
    write(
        tmp_path,
        TRANSFER_HINTS_FILE,
        [
            "Sample  SWEEP,out,test-savings",  # 2: good, normalized
            "sample fx,conversion,",  # 3: good
            "sample undo,sideways,",  # 4: not a direction
            ",in,test-savings",  # 5: no pattern
            "sample move,in,no-such-account",  # 6: unknown counterpart
            "sample undo,reversal,test-savings",  # 7: a reversal names none
            "sample in,in,",  # 8: good, any own account
        ],
    )
    uow = UnitOfWork(owner_conn)
    registry = load_accounts(uow, tmp_path)

    loaded = load_transfer_hints(uow, tmp_path, registry)

    assert [(h.line_no, h.row.pattern, h.row.direction) for h in loaded.hints] == [
        (2, "sample sweep", "out"),
        (3, "sample fx", "conversion"),
        (8, "sample in", "in"),
    ]
    assert loaded.rows[0].counterpart_account == "test-savings"
    assert loaded.rows[2].counterpart_account is None
    reasons = stored_errors(owner_conn)
    assert sorted(reasons) == [4, 5, 6, 7]
    assert sorted({e.line_no for e in loaded.errors}) == [4, 5, 6, 7]
    assert reasons[4].startswith("direction:")
    assert reasons[5].startswith("pattern:")
    assert reasons[6] == "counterpart_account: no loaded account has this slug"
    assert "names no counterpart" in reasons[7]
    # A reason names the column, never the cell's value.
    assert not any("sample" in reason for reason in reasons.values())


def test_a_wrong_header_is_one_error_on_line_1(
    owner_conn: Connection, tmp_path: Path
) -> None:
    manual = tmp_path / "manual"
    manual.mkdir()
    (manual / TRANSFER_HINTS_FILE).write_text(
        "pattern,counterpart_account,direction\nsample sweep,,out\n", encoding="utf-8"
    )
    uow = UnitOfWork(owner_conn)

    loaded = load_transfer_hints(uow, tmp_path, load_accounts(uow, tmp_path))

    assert loaded.hints == ()
    assert list(stored_errors(owner_conn)) == [1]
