"""The Downloads filer through ``finance.document.file`` (FR-2, Key decision 2, AC-2).

Every test builds a temporary Downloads and a temporary archive, points
``finance.paths.downloads`` and ``finance.paths.archive`` at them, dispatches the
operation on the test's rolled-back owner connection, and reads both directories and
the ``document`` table back. A "PDF" here is layout text, and pdftotext is stubbed by a
reader that returns a file's bytes as text (``NoTextLayer`` for bytes that are not
UTF-8). Every value is synthetic.
"""

from __future__ import annotations

import csv
import dataclasses
import datetime as dt
import hashlib
import os
from collections.abc import Callable
from pathlib import Path

import pytest
from sqlalchemy import Connection, func, select

from rheo_finance.context import FinanceContext, UnitOfWork
from rheo_finance.contracts import Role
from rheo_finance.documents import filer
from rheo_finance.documents.registry import FileOut, PathCheck
from rheo_finance.manual.accounts import IDENTIFIERS_FILE
from rheo_finance.manual.headers import MANUAL_FILES
from rheo_finance.operations import dispatch
from rheo_finance.parsers.base import NoTextLayer
from rheo_finance.settings import parse_settings
from rheo_finance.storage.schema import document

MakeContext = Callable[[Role], FinanceContext]
OPERATION = "finance.document.file"
FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "synthetic"
# The test context's fake clock.
NOW = dt.datetime(2026, 1, 15, 12, 0, tzinfo=dt.UTC)
# Each fixture's filing date: period end, bill date, last row's date.
STATEMENT = (FIXTURES / "vancity_statement_2026.txt").read_bytes()  # 2026-01-10
BILL = (FIXTURES / "virgin_bill.txt").read_bytes()  # 2026-02-02
EXPORT = (FIXTURES / "vancity_chequing_jump.csv").read_bytes()  # 2026-02-16


@pytest.fixture(autouse=True)
def stub_pdftotext(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """pdftotext, stubbed: the file's bytes as text. Returns the names it read."""
    read: list[str] = []

    def as_text(path: Path) -> str:
        read.append(path.name)
        try:
            return path.read_bytes().decode("utf-8")
        except UnicodeDecodeError:
            raise NoTextLayer("not text") from None

    monkeypatch.setattr(filer, "pdf_text", as_text)
    return read


@dataclasses.dataclass
class Dirs:
    downloads: Path
    archive: Path
    data: Path


@pytest.fixture
def dirs(tmp_path: Path) -> Dirs:
    found = Dirs(tmp_path / "Downloads", tmp_path / "archive", tmp_path)
    found.downloads.mkdir()
    found.archive.mkdir()
    return found


def download(dirs: Dirs, name: str, content: bytes, *, age: dt.timedelta) -> Path:
    """Write ``name`` into Downloads with an mtime ``age`` before the fake clock."""
    path = dirs.downloads / name
    path.write_bytes(content)
    stamp = (NOW - age).timestamp()
    os.utime(path, (stamp, stamp))
    return path


HOUR = dt.timedelta(hours=1)


def file_op(rolled_back_context: MakeContext, dirs: Dirs) -> FileOut:
    ctx = dataclasses.replace(
        rolled_back_context(Role.OWNER),
        settings=parse_settings(
            f'[finance.paths]\narchive = "{dirs.archive}"\n'
            f'downloads = "{dirs.downloads}"\n'
        ),
    )
    out = dispatch(OPERATION, {}, Role.OWNER, ctx=ctx)
    assert isinstance(out, FileOut)
    return out


def tree(root: Path) -> dict[str, str]:
    """Every file under ``root`` by relative path, with its sha256."""
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def downloads_state(dirs: Dirs) -> dict[str, tuple[str, int]]:
    """Every Downloads file with its sha256 and mtime, to prove nothing changed."""
    return {
        path.name: (sha(path.read_bytes()), path.stat().st_mtime_ns)
        for path in sorted(dirs.downloads.rglob("*"))
        if path.is_file()
    }


def registered_paths(conn: Connection) -> list[str]:
    return sorted(str(path) for path in conn.scalars(select(document.c.path)))


def document_count(conn: Connection) -> int:
    return int(conn.scalar(select(func.count()).select_from(document)) or 0)


def sha(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


# --- filed: copied to the dated name, the original untouched --------------------------


def test_a_statement_is_copied_under_its_date_and_the_original_stays(
    rolled_back_context: MakeContext, dirs: Dirs, owner_conn: Connection
) -> None:
    download(dirs, "statement export 999.csv", EXPORT, age=HOUR)
    download(dirs, "e-statement.pdf", STATEMENT, age=HOUR)
    download(dirs, "my bill.pdf", BILL, age=HOUR)
    before = downloads_state(dirs)

    out = file_op(rolled_back_context, dirs)

    assert tree(dirs.archive) == {
        "vancity/2026-01-10.pdf": sha(STATEMENT),
        "vancity/2026-02-16.csv": sha(EXPORT),
        "virgin-plus/2026-02-02.pdf": sha(BILL),
    }
    assert downloads_state(dirs) == before
    assert (out.downloads.scanned, out.downloads.filed, out.downloads.known) == (
        True, 3, 0,
    )  # fmt: skip
    # The walk in the same run registers what the filer copied.
    assert out.registered == 3
    assert registered_paths(owner_conn) == sorted(tree(dirs.archive))
    # No Downloads name reaches the output or a stored path.
    for name in before:
        assert name not in out.model_dump_json()
        assert all(name not in path for path in registered_paths(owner_conn))


def test_an_all_digit_sha8_still_files_under_a_name_the_check_accepts(
    rolled_back_context: MakeContext, dirs: Dirs
) -> None:
    # About 2.3% of files have a sha256 whose first 8 hex digits are all digits; a
    # name holding that next to a date would be an 8-digit run the check refuses.
    content = next(
        candidate
        for padding in range(10_000)
        if sha(candidate := EXPORT + b"\n" * padding)[:8].isdigit()
    )
    download(dirs, "export.csv", content, age=HOUR)

    out = file_op(rolled_back_context, dirs)

    assert tree(dirs.archive) == {"vancity/2026-02-16.csv": sha(content)}
    assert PathCheck(()).allows("vancity/2026-02-16.csv")
    assert (out.downloads.filed, out.downloads.refused) == (1, 0)


# --- the monitors' files --------------------------------------------------------------


def test_monitor_files_are_skipped_and_reported_after_a_day(
    rolled_back_context: MakeContext, dirs: Dirs, stub_pdftotext: list[str]
) -> None:
    download(dirs, "VCTY_999_old.pdf", STATEMENT, age=25 * HOUR)
    download(dirs, "VCTY_999_day.pdf", STATEMENT + b" ", age=24 * HOUR)
    download(dirs, "VCTY_999_new.pdf", STATEMENT + b"  ", age=23 * HOUR)
    download(dirs, "VirginPDF999.pdf", BILL, age=48 * HOUR)
    before = downloads_state(dirs)

    out = file_op(rolled_back_context, dirs)

    assert tree(dirs.archive) == {}
    assert stub_pdftotext == []  # never opened
    assert downloads_state(dirs) == before
    assert (out.downloads.monitor_skipped, out.downloads.monitor_waiting) == (4, 3)
    assert out.downloads.filed == 0
    # Status reads the same count without running the filer.
    assert filer.monitor_files_waiting(dirs.downloads, NOW) == 3
    assert filer.monitor_files_waiting(dirs.downloads, NOW + 2 * HOUR) == 4
    assert filer.monitor_files_waiting(dirs.data / "no-downloads", NOW) == 0


# --- clash, refusal, idempotence ------------------------------------------------------


def test_a_clash_with_other_bytes_gets_a_numbered_name(
    rolled_back_context: MakeContext, dirs: Dirs
) -> None:
    earlier = dirs.archive / "vancity" / "2026-02-16.csv"
    earlier.parent.mkdir()
    earlier.write_bytes(b"other bytes already filed\n")
    download(dirs, "a.csv", EXPORT, age=HOUR)
    download(dirs, "b.csv", EXPORT + b"\n", age=HOUR)

    out = file_op(rolled_back_context, dirs)

    assert tree(dirs.archive) == {
        "vancity/2026-02-16.csv": sha(b"other bytes already filed\n"),
        "vancity/2026-02-16-2.csv": sha(EXPORT),
        "vancity/2026-02-16-3.csv": sha(EXPORT + b"\n"),
    }
    assert out.downloads.filed == 2


def write_identifier(dirs: Dirs, identifier: str) -> None:
    manual = dirs.data / "manual"
    manual.mkdir(exist_ok=True)
    (manual / IDENTIFIERS_FILE).write_text(
        ",".join(MANUAL_FILES[IDENTIFIERS_FILE])
        + f"\nchequing,{identifier},number,,\n",
        encoding="utf-8",
    )


def test_a_target_name_carrying_an_identifier_is_refused(
    rolled_back_context: MakeContext, dirs: Dirs, owner_conn: Connection
) -> None:
    # A registered number the date's digits spell: 2026-02-16 holds 0216.
    write_identifier(dirs, "0216")
    download(dirs, "export.csv", EXPORT, age=HOUR)
    download(dirs, "e-statement.pdf", STATEMENT, age=HOUR)
    before = downloads_state(dirs)

    out = file_op(rolled_back_context, dirs)

    assert tree(dirs.archive) == {"vancity/2026-01-10.pdf": sha(STATEMENT)}
    assert downloads_state(dirs) == before
    assert (out.downloads.filed, out.downloads.refused) == (1, 1)
    assert registered_paths(owner_conn) == ["vancity/2026-01-10.pdf"]
    assert "0216" not in out.model_dump_json()


def test_the_same_bytes_twice_add_nothing(
    rolled_back_context: MakeContext, dirs: Dirs, owner_conn: Connection
) -> None:
    download(dirs, "export.csv", EXPORT, age=HOUR)
    first = file_op(rolled_back_context, dirs)
    archived, count = tree(dirs.archive), document_count(owner_conn)
    # The same bytes again, under the old name and a new one.
    download(dirs, "export (1).csv", EXPORT, age=HOUR)

    second = file_op(rolled_back_context, dirs)

    assert first.downloads.filed == 1
    assert (second.downloads.filed, second.downloads.known) == (0, 2)
    assert tree(dirs.archive) == archived == {"vancity/2026-02-16.csv": sha(EXPORT)}
    assert document_count(owner_conn) == count == 1


def test_bytes_already_at_their_name_are_known_before_the_walk_registers_them(
    rolled_back_context: MakeContext, dirs: Dirs
) -> None:
    placed = dirs.archive / "vancity" / "2026-02-16.csv"
    placed.parent.mkdir()
    placed.write_bytes(EXPORT)
    download(dirs, "export.csv", EXPORT, age=HOUR)

    out = file_op(rolled_back_context, dirs)

    assert tree(dirs.archive) == {"vancity/2026-02-16.csv": sha(EXPORT)}
    assert (out.downloads.filed, out.downloads.known) == (0, 1)


# --- unrecognised and passed over -----------------------------------------------------


def test_unrecognised_files_stay_untouched_and_are_listed(
    rolled_back_context: MakeContext, dirs: Dirs, owner_conn: Connection
) -> None:
    download(dirs, "notes.csv", b"a,b\n1,2\n", age=HOUR)
    download(dirs, "letter.pdf", b"Dear owner, nothing to file here.\n", age=HOUR)
    download(dirs, "scan.pdf", b"\xff\xfe\x00 not text", age=HOUR)
    download(dirs, "song.mp3", b"not a candidate", age=HOUR)
    download(dirs, ".hidden.csv", EXPORT, age=HOUR)
    (dirs.downloads / "folder").mkdir()
    (dirs.downloads / "folder" / "inside.csv").write_bytes(EXPORT)
    (dirs.downloads / "link.csv").symlink_to(dirs.downloads / "folder" / "inside.csv")
    before = downloads_state(dirs)

    out = file_op(rolled_back_context, dirs)

    assert tree(dirs.archive) == {}
    assert downloads_state(dirs) == before
    assert out.downloads.unrecognised == 3
    assert out.downloads.filed == 0
    assert document_count(owner_conn) == 0
    for name in ("notes.csv", "letter.pdf", "scan.pdf"):
        assert name not in out.model_dump_json()


def test_a_matching_file_with_unreadable_fields_is_unrecognised_and_the_scan_goes_on(
    rolled_back_context: MakeContext, dirs: Dirs
) -> None:
    # The layout matches a statement, but the period end is not a real date, so the
    # rule raises ValueError instead of NotRecognised. Files sort by name: the bad one
    # comes first, and the good ones after it must still be filed.
    broken = STATEMENT.replace(b"to 10 JAN 2026", b"to 31 FEB 2026")
    assert broken != STATEMENT
    download(dirs, "a-broken.pdf", broken, age=HOUR)
    download(dirs, "b-statement.pdf", STATEMENT, age=HOUR)
    download(dirs, "c-export.csv", EXPORT, age=HOUR)
    before = downloads_state(dirs)

    out = file_op(rolled_back_context, dirs)

    assert tree(dirs.archive) == {
        "vancity/2026-01-10.pdf": sha(STATEMENT),
        "vancity/2026-02-16.csv": sha(EXPORT),
    }
    assert downloads_state(dirs) == before
    assert (out.downloads.filed, out.downloads.unrecognised) == (2, 1)
    assert "a-broken" not in out.model_dump_json()


def test_a_csv_cell_over_the_reader_limit_is_unrecognised_and_the_scan_goes_on(
    rolled_back_context: MakeContext, dirs: Dirs
) -> None:
    # csv.Error is not a ValueError: a field over the reader's size limit must still
    # mean "this rule cannot date this file", not abort the scan.
    giant = EXPORT.replace(b"POS PURCHASE", b"x" * 200_000, 1)
    assert len(giant) > csv.field_size_limit()
    download(dirs, "a-giant.csv", giant, age=HOUR)
    download(dirs, "b-export.csv", EXPORT, age=HOUR)
    before = downloads_state(dirs)

    out = file_op(rolled_back_context, dirs)

    assert tree(dirs.archive) == {"vancity/2026-02-16.csv": sha(EXPORT)}
    assert downloads_state(dirs) == before
    assert (out.downloads.filed, out.downloads.unrecognised) == (1, 1)
    assert "a-giant" not in out.model_dump_json()


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory permissions")
def test_a_downloads_directory_that_cannot_be_listed_does_not_stop_the_archive_walk(
    rolled_back_context: MakeContext, dirs: Dirs, owner_conn: Connection
) -> None:
    placed = dirs.archive / "vancity" / "2026-01-10.pdf"
    placed.parent.mkdir()
    placed.write_bytes(STATEMENT)
    download(dirs, "export.csv", EXPORT, age=HOUR)
    dirs.downloads.chmod(0o000)
    try:
        out = file_op(rolled_back_context, dirs)
        waiting = filer.monitor_files_waiting(dirs.downloads, NOW)
    finally:
        dirs.downloads.chmod(0o700)

    assert out.downloads.scanned is False
    assert out.downloads.filed == 0
    assert waiting == 0
    assert out.registered == 1  # the walk still ran
    assert registered_paths(owner_conn) == ["vancity/2026-01-10.pdf"]
    assert tree(dirs.archive) == {"vancity/2026-01-10.pdf": sha(STATEMENT)}


def test_bytes_registered_under_a_legacy_archive_name_are_known(
    rolled_back_context: MakeContext, dirs: Dirs, owner_conn: Connection
) -> None:
    legacy = dirs.archive / "vancity" / "statement-from-the-old-days.pdf"
    legacy.parent.mkdir()
    legacy.write_bytes(STATEMENT)
    first = file_op(rolled_back_context, dirs)  # the walk registers the legacy name
    assert first.registered == 1
    archived = tree(dirs.archive)
    download(dirs, "e-statement.pdf", STATEMENT, age=HOUR)

    out = file_op(rolled_back_context, dirs)

    assert (out.downloads.filed, out.downloads.known) == (0, 1)
    assert tree(dirs.archive) == archived  # no second copy under the dated name
    assert registered_paths(owner_conn) == ["vancity/statement-from-the-old-days.pdf"]


def test_two_identical_downloads_in_one_scan_are_read_once(
    rolled_back_context: MakeContext, dirs: Dirs, stub_pdftotext: list[str]
) -> None:
    download(dirs, "a.pdf", STATEMENT, age=HOUR)
    download(dirs, "b.pdf", STATEMENT, age=HOUR)

    out = file_op(rolled_back_context, dirs)

    assert (out.downloads.filed, out.downloads.known) == (1, 1)
    assert stub_pdftotext == ["a.pdf"]  # the second copy is known before text is read
    assert tree(dirs.archive) == {"vancity/2026-01-10.pdf": sha(STATEMENT)}


def test_the_returned_list_names_the_unrecognised_and_refused_files(
    owner_conn: Connection, dirs: Dirs
) -> None:
    download(dirs, "notes.csv", b"a,b\n1,2\n", age=HOUR)
    download(dirs, "export.csv", EXPORT, age=HOUR)

    filed = filer.file_downloads(
        UnitOfWork(owner_conn), dirs.downloads, dirs.archive, PathCheck(["0216"]), NOW
    )

    assert filed.unrecognised == ("notes.csv",)
    assert filed.refused == ("export.csv",)
    assert tree(dirs.archive) == {}


def test_a_missing_downloads_directory_files_nothing(
    rolled_back_context: MakeContext, dirs: Dirs
) -> None:
    dirs.downloads.rmdir()

    out = file_op(rolled_back_context, dirs)

    assert out.downloads.scanned is False
    assert out.downloads.filed == 0
