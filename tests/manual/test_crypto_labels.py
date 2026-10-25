"""``crypto_labels.csv`` through the public stage 2 loader (FR-10, FR-15(b); AC-10 bad
rows): rows load into ``crypto_label`` keyed by the line key their line ref names, and
a bad row is reported by its line number while every other row loads. The line refs
name invented ids.
"""

from __future__ import annotations

import datetime as dt
import uuid
from pathlib import Path

from sqlalchemy import Connection, select

from rheo_finance.context import UnitOfWork
from rheo_finance.manual.crypto_labels import CRYPTO_LABELS_FILE, CryptoLabelRow
from rheo_finance.manual.headers import MANUAL_FILES
from rheo_finance.manual.stage2 import load_crypto_labels
from rheo_finance.storage.ids import derived_id
from rheo_finance.storage.schema import crypto_label, manual_entry_error

KEPT = derived_id(dt.date(2025, 1, 2), ("sample", "kept"))
OTHER = derived_id(dt.date(2025, 1, 2), ("sample", "other"))
TWICE = derived_id(dt.date(2025, 1, 2), ("sample", "twice"))


def ref(key: uuid.UUID, ref_type: str = "txn") -> str:
    return f"finance.{ref_type}:{key}"


def write(data: Path, rows: list[str]) -> None:
    manual = data / "manual"
    manual.mkdir(exist_ok=True)
    header = ",".join(MANUAL_FILES[CRYPTO_LABELS_FILE])
    (manual / CRYPTO_LABELS_FILE).write_text(
        "\n".join([header, *rows]) + "\n", encoding="utf-8"
    )


def stored(conn: Connection) -> dict[uuid.UUID, tuple[str, str | None]]:
    rows = conn.execute(
        select(crypto_label.c.line_key_id, crypto_label.c.label, crypto_label.c.note)
    )
    return {row.line_key_id: (row.label, row.note) for row in rows}


def errors(conn: Connection) -> dict[int, str]:
    rows = conn.execute(
        select(manual_entry_error.c.line_no, manual_entry_error.c.reason).where(
            manual_entry_error.c.file == CRYPTO_LABELS_FILE
        )
    )
    return {int(line): str(reason) for line, reason in rows}


def test_the_row_model_has_the_files_columns() -> None:
    assert tuple(CryptoLabelRow.model_fields) == MANUAL_FILES[CRYPTO_LABELS_FILE]


def test_labels_load_by_line_key_and_a_bad_row_is_reported(
    owner_conn: Connection, tmp_path: Path
) -> None:
    uow = UnitOfWork(owner_conn)
    write(
        tmp_path,
        [
            f"{ref(KEPT)},own_untracked,sample note",
            f"{ref(OTHER)},other,",
            f"{ref(TWICE)},other,",
            f"{ref(TWICE)},own_untracked,",
            f"{ref(KEPT, 'account')},other,",
            f"finance.txn:{uuid.UUID(int=7)},other,",
            "not a ref,other,",
            f"{ref(OTHER)}-x,sold,",
        ],
    )

    found = load_crypto_labels(uow, tmp_path)

    assert stored(owner_conn) == {
        KEPT: ("own_untracked", "sample note"),
        OTHER: ("other", None),
    }
    reasons = errors(owner_conn)
    assert sorted(reasons) == [4, 5, 6, 7, 8, 9]
    assert reasons[4] == reasons[5] == "line_ref: another row labels it"
    for line in (6, 7, 8):
        assert reasons[line].startswith("line_ref")
    assert "label" in reasons[9]
    assert len(found.rows) == 2
