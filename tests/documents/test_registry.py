"""The document registry and archive walk through ``finance.document.file`` (FR-2,
FR-9, AC-2, AC-9 image part).

Every test builds a temporary archive of synthetic files, points
``finance.paths.archive`` at it, dispatches the operation on the test's rolled-back
owner connection and reads the tables back. Registered identifiers come from the
manual files through ``manual.stage2.load_accounts``. Every value is synthetic.
"""

from __future__ import annotations

import dataclasses
import hashlib
import logging
import os
from collections.abc import Callable
from pathlib import Path

import pytest
from sqlalchemy import Connection, func, select, text

from rheo_finance.context import FinanceContext, UnitOfWork
from rheo_finance.contracts import Role
from rheo_finance.documents.registry import (
    DOCUMENT_STATUSES,
    DocumentRefused,
    FileOut,
)
from rheo_finance.manual.accounts import ACCOUNTS_FILE, IDENTIFIERS_FILE
from rheo_finance.manual.headers import MANUAL_FILES
from rheo_finance.manual.stage2 import load_accounts
from rheo_finance.operations import RoleRefused, dispatch
from rheo_finance.settings import parse_settings
from rheo_finance.storage.schema import account_identifier, archive_skip, document

MakeContext = Callable[[Role], FinanceContext]
OPERATION = "finance.document.file"
# 2021-03-04, an mtime no test run produces by accident.
OLD_MTIME_NS = 1_614_816_000_000_000_000


def write_file(archive: Path, relative: str, content: bytes) -> Path:
    path = archive / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    os.utime(path, ns=(OLD_MTIME_NS, OLD_MTIME_NS))
    return path


def file_op(
    rolled_back_context: MakeContext, archive: Path, role: Role = Role.OWNER
) -> FileOut:
    ctx = dataclasses.replace(
        rolled_back_context(role),
        settings=parse_settings(f'[finance.paths]\narchive = "{archive}"\n'),
    )
    out = dispatch(OPERATION, {}, role, ctx=ctx)
    assert isinstance(out, FileOut)
    return out


def documents(conn: Connection) -> list[tuple[str, str, str, str, str, str]]:
    rows = conn.execute(
        select(
            document.c.path,
            document.c.issuer,
            document.c.kind,
            document.c.format,
            document.c.status,
            document.c.extraction,
        ).order_by(document.c.path, document.c.issuer)
    )
    return [
        (str(path), str(issuer), str(kind), str(fmt), str(status), str(extraction))
        for path, issuer, kind, fmt, status, extraction in rows
    ]


def document_count(conn: Connection) -> int:
    return int(conn.scalar(select(func.count()).select_from(document)) or 0)


def skips(conn: Connection) -> list[tuple[str, str, int]]:
    rows = conn.execute(
        select(archive_skip.c.directory, archive_skip.c.reason, archive_skip.c.count)
    )
    return sorted((str(d), str(r), int(c)) for d, r, c in rows)


def snapshot(archive: Path) -> dict[str, tuple[str, int, int]]:
    """Every entry under ``archive``: its sha256 (files), mtime and mode."""
    found: dict[str, tuple[str, int, int]] = {}
    for path in sorted(archive.rglob("*")):
        info = path.lstat()
        digest = (
            hashlib.sha256(path.read_bytes()).hexdigest()
            if path.is_file() and not path.is_symlink()
            else ""
        )
        found[path.relative_to(archive).as_posix()] = (
            digest,
            info.st_mtime_ns,
            info.st_mode,
        )
    return found


@pytest.fixture
def archive(tmp_path: Path) -> Path:
    root = tmp_path / "archive"
    root.mkdir()
    return root


@pytest.fixture
def load_identifiers(owner_conn: Connection, tmp_path: Path) -> Callable[[], None]:
    """Load three synthetic accounts, one identifier of each matchable kind, through
    the stage 2 loader into ``account_identifier`` on the test's connection."""

    def load() -> None:
        data = tmp_path / "data"
        manual = data / "manual"
        manual.mkdir(parents=True)
        accounts = [
            ",".join(MANUAL_FILES[ACCOUNTS_FILE]),
            "test-chequing,Test chequing,Test Credit Union,chequing,CAD,monthly,"
            + "," * 8,
            "test-card,Test card,Example Bank,credit_card,CAD,monthly," + "," * 8,
            "test-wallet,Test wallet,Test Exchange,crypto_wallet,BTC,none," + "," * 8,
        ]
        identifiers = [
            ",".join(MANUAL_FILES[IDENTIFIERS_FILE]),
            "test-chequing,9991234,number,,",
            "test-card,9999 99** **** 4321,masked_card,,",
            "test-wallet,Synthetic Pot,portfolio_name,,",
        ]
        (manual / ACCOUNTS_FILE).write_text("\n".join(accounts) + "\n")
        (manual / IDENTIFIERS_FILE).write_text("\n".join(identifiers) + "\n")
        registry = load_accounts(UnitOfWork(owner_conn), data)
        assert registry.errors == ()
        assert len(registry.identifiers) == 4  # the three and the built-in one

    return load


def test_adoption_leaves_every_file_byte_identical_with_its_mtime(
    owner_conn: Connection, rolled_back_context: MakeContext, archive: Path
) -> None:
    write_file(archive, "vancity/2024-01-31.pdf", b"%PDF synthetic statement\n")
    write_file(archive, "virgin-plus/2024-02-10.pdf", b"%PDF synthetic bill\n")
    write_file(archive, "fortisbc/2024-03-01-bill.jpeg", b"\xff\xd8 synthetic photo")
    write_file(archive, "vancity/9999123456.pdf", b"skipped, never read\n")
    write_file(archive, "elsewhere/notes.txt", b"unmapped\n")
    before = snapshot(archive)

    out = file_op(rolled_back_context, archive)

    assert out.registered == 3
    assert snapshot(archive) == before


def test_each_file_is_registered_with_its_hash_issuer_kind_format_and_status(
    owner_conn: Connection, rolled_back_context: MakeContext, archive: Path
) -> None:
    files = {
        "vancity/2024-01-31.pdf": b"%PDF statement",
        "vancity/transactions-2024-01-01_to_2024-01-31.csv": b"9990,synthetic\n",
        "vancity/api/2024-01-31-accounts.json": b'{"synthetic": 1}',
        "virgin-plus/2024-02-10.pdf": b"%PDF bill",
        "fortisbc/2024-03-01-bill.jpeg": b"\xff\xd8 photo",
        "fortisbc/2024-04-01-bill.HEIC": b"heic photo",
        "coinstats/portfolio-coins-2024-05-01.json": b'{"coins": []}',
        "coinstats/transactions-2024-05-01.json": b'{"transactions": []}',
        "blofin/2024-06-30.csv": b"9990,trade\n",
        "invoices-received/example-vendor/inv9990.pdf": b"%PDF invoice",
        "receipts/example-shop/2024-07-01-receipt.pdf": b"%PDF receipt",
        "receipts/2024-07-02.png": b"\x89PNG receipt photo",
        "_mail/example.org/2024-08-01-9999abcd.eml": b"From: a@example.org\n",
        "_drops/2024-08-02-9999abce.json": b'{"drop": true}',
        "cra/2023-t4.pdf": b"%PDF slip",
        "wise/statement": b"no extension",
    }
    for relative, content in files.items():
        write_file(archive, relative, content)

    out = file_op(rolled_back_context, archive)

    assert out.registered == len(files)
    assert documents(owner_conn) == [
        ("_drops/2024-08-02-9999abce.json", "_drops", "drop_record", "json",
         "filed", "transcription"),
        ("_mail/example.org/2024-08-01-9999abcd.eml", "_mail", "email", "eml",
         "filed", "none"),
        ("blofin/2024-06-30.csv", "blofin", "trading_export", "csv", "filed", "none"),
        ("coinstats/portfolio-coins-2024-05-01.json", "coinstats",
         "holdings_snapshot", "json", "filed", "none"),
        ("coinstats/transactions-2024-05-01.json", "coinstats", "export", "json",
         "filed", "none"),
        ("cra/2023-t4.pdf", "cra", "cra_slip", "pdf", "filed", "none"),
        ("fortisbc/2024-03-01-bill.jpeg", "fortisbc", "image", "jpeg",
         "needs_manual_entry", "none"),
        ("fortisbc/2024-04-01-bill.HEIC", "fortisbc", "image", "heic",
         "needs_manual_entry", "none"),
        ("invoices-received/example-vendor/inv9990.pdf",
         "invoices-received/example-vendor", "invoice", "pdf", "filed", "none"),
        ("receipts/2024-07-02.png", "receipts", "image", "png",
         "needs_manual_entry", "none"),
        ("receipts/example-shop/2024-07-01-receipt.pdf", "receipts/example-shop",
         "receipt", "pdf", "filed", "none"),
        ("vancity/2024-01-31.pdf", "vancity", "statement", "pdf", "filed", "none"),
        ("vancity/api/2024-01-31-accounts.json", "vancity", "api_response", "json",
         "filed", "none"),
        ("vancity/transactions-2024-01-01_to_2024-01-31.csv", "vancity", "export",
         "csv", "filed", "none"),
        ("virgin-plus/2024-02-10.pdf", "virgin-plus", "bill", "pdf", "filed", "none"),
        ("wise/statement", "wise", "statement", "none", "filed", "none"),
    ]  # fmt: skip
    hashes = {
        str(path): str(sha)
        for path, sha in owner_conn.execute(select(document.c.path, document.c.sha256))
    }
    for relative, content in files.items():
        assert hashes[relative] == hashlib.sha256(content).hexdigest()
    statuses = set(owner_conn.scalars(select(document.c.status)))
    assert statuses <= set(DOCUMENT_STATUSES)


def test_the_same_bytes_filed_twice_leave_the_count_unchanged(
    owner_conn: Connection, rolled_back_context: MakeContext, archive: Path
) -> None:
    write_file(archive, "vancity/2024-01-31.pdf", b"%PDF one")
    write_file(archive, "mbna/2024-01-31.pdf", b"%PDF two")

    first = file_op(rolled_back_context, archive)
    count = document_count(owner_conn)
    second = file_op(rolled_back_context, archive)

    assert (first.registered, first.known) == (2, 0)
    assert (second.registered, second.known) == (0, 2)
    assert document_count(owner_conn) == count == 2


def test_the_same_bytes_at_two_paths_are_one_document(
    owner_conn: Connection, rolled_back_context: MakeContext, archive: Path
) -> None:
    write_file(archive, "wise/2024-01-31.pdf", b"%PDF same bytes")
    write_file(archive, "wise/2024-01-31-copy.pdf", b"%PDF same bytes")

    out = file_op(rolled_back_context, archive)

    assert (out.registered, out.duplicates) == (1, 1)
    assert [row[0] for row in documents(owner_conn)] == ["wise/2024-01-31-copy.pdf"]


def test_a_document_whose_bytes_moved_follows_them(
    owner_conn: Connection, rolled_back_context: MakeContext, archive: Path
) -> None:
    old = write_file(archive, "receipts/example-shop/receipt.pdf", b"%PDF receipt")
    file_op(rolled_back_context, archive)
    old.rename(archive / "receipts/example-shop/2024-07-01.pdf")

    out = file_op(rolled_back_context, archive)

    assert (out.registered, out.known, out.moved) == (0, 0, 1)
    assert [row[0] for row in documents(owner_conn)] == [
        "receipts/example-shop/2024-07-01.pdf"
    ]


# Paths the adopted-path check refuses: an identifier of each kind, a long digit run
# and an 8-digit run that is not a calendar date.
SKIPPED = {
    "vancity/statement-9991234.pdf": "number identifier",
    "vancity/999-1234-2024.pdf": "number identifier split by a separator",
    "mbna/9999-99xx-xxxx-4321.pdf": "masked card, other mask characters",
    "coinstats/synthetic_pot-2024-01-31.json": "portfolio name",
    "wise/99912345678.pdf": "11-digit run",
    "wise/99991399.pdf": "8 digits, month 13",
    "wise/99991231.pdf": "a YYYYMMDD shape whose year is out of range",
}
ADOPTED = {
    "vancity/20240131.pdf": "8 digits that are a date",
    "vancity/2024-01-31.pdf": "a hyphenated date",
    "mbna/2024-02-29-4321.pdf": "last four alone is not the masked card",
    "vancity/cash-2024.pdf": "the built-in internal identifier is not matched",
    "vancity/1999-1234.pdf": "the number's digits only from inside a run",
}


def test_an_identifier_bearing_path_is_skipped_counted_and_stored_nowhere(
    owner_conn: Connection,
    rolled_back_context: MakeContext,
    archive: Path,
    load_identifiers: Callable[[], None],
    caplog: pytest.LogCaptureFixture,
) -> None:
    load_identifiers()
    for number, relative in enumerate([*SKIPPED, *ADOPTED]):
        write_file(archive, relative, f"synthetic file {number}".encode())

    with caplog.at_level(logging.DEBUG):
        out = file_op(rolled_back_context, archive)

    assert sorted(row[0] for row in documents(owner_conn)) == sorted(ADOPTED)
    assert skips(owner_conn) == [
        ("coinstats", "identifier_in_path", 1),
        ("mbna", "identifier_in_path", 1),
        ("vancity", "identifier_in_path", 2),
        ("wise", "identifier_in_path", 3),
    ]
    assert [(s.directory, s.reason, s.count) for s in out.skipped] == skips(owner_conn)

    # No skipped path, nor its file name, is in any table or view, the output or a
    # log record.
    needles = {
        part for relative in SKIPPED for part in (relative, relative.split("/")[1])
    }
    tables = owner_conn.scalars(
        text(
            "SELECT table_name FROM information_schema.tables"
            " WHERE table_schema = 'finance'"
        )
    ).all()
    assert "document" in tables and "archive_skip" in tables
    stored = [
        str(row)
        for table in tables
        for row in owner_conn.scalars(
            text(f'SELECT CAST(t AS text) FROM finance."{table}" t')
        )
    ]
    haystacks = [*stored, out.model_dump_json(), *caplog.messages]
    found = [needle for needle in needles if any(needle in hay for hay in haystacks)]
    assert found == []


def test_identifiers_in_the_manual_file_are_refused_before_stage_2_has_run(
    owner_conn: Connection,
    rolled_back_context: MakeContext,
    archive: Path,
    tmp_path: Path,
) -> None:
    # The context's data directory is tmp_path; account_identifier stays empty.
    manual = tmp_path / "manual"
    manual.mkdir()
    (manual / IDENTIFIERS_FILE).write_text(
        "\n".join(
            [
                ",".join(MANUAL_FILES[IDENTIFIERS_FILE]),
                "test-chequing,9991234,number,,",
                "test-savings,99912,number,,",
                # Invalid rows still name real identifiers.
                "test-other,Synthetic Pot,portfolio_name,not-a-date,",
                "test-short,9997777",
                "paid-outside,CASH,internal,,",
            ]
        )
        + "\n"
    )
    skipped = [
        "vancity/statement-9991234.pdf",
        "vancity/(999) 1234.pdf",
        "vancity/999,1234.pdf",
        "vancity/999\u20131234.pdf",
        "vancity/999+1234.pdf",
        "vancity/999/1234.pdf",
        "vancity/ref-7999127.pdf",
        "coinstats/synthetic_pot.json",
        "mbna/9997777.pdf",
    ]
    adopted = ["vancity/cash-2024.pdf", "vancity/2024-01-31.pdf"]
    for number, relative in enumerate([*skipped, *adopted]):
        write_file(archive, relative, f"synthetic file {number}".encode())
    assert owner_conn.scalar(select(func.count()).select_from(account_identifier)) == 0

    out = file_op(rolled_back_context, archive)

    assert sorted(row[0] for row in documents(owner_conn)) == sorted(adopted)
    assert skips(owner_conn) == [
        ("coinstats", "identifier_in_path", 1),
        ("mbna", "identifier_in_path", 1),
        ("vancity", "identifier_in_path", 7),
    ]
    stored = " ".join(
        f"{path} {issuer}"
        for path, issuer in owner_conn.execute(
            select(document.c.path, document.c.issuer)
        )
    )
    assert "1234" not in stored and "pot" not in stored and "7777" not in stored
    assert "1234" not in out.model_dump_json()


def test_an_unreadable_file_or_directory_is_counted_and_the_walk_goes_on(
    owner_conn: Connection, rolled_back_context: MakeContext, archive: Path
) -> None:
    locked_file = write_file(archive, "vancity/2024-01-31.pdf", b"%PDF locked")
    write_file(archive, "mbna/private-sub/2024-01-31.pdf", b"%PDF locked dir")
    locked_dir = archive / "mbna/private-sub"
    write_file(archive, "virgin-plus/2024-02-10.pdf", b"%PDF readable")
    locked_file.chmod(0)
    locked_dir.chmod(0)
    try:
        out = file_op(rolled_back_context, archive)
    finally:
        locked_dir.chmod(0o755)
        locked_file.chmod(0o644)

    assert out.registered == 1
    assert [row[0] for row in documents(owner_conn)] == ["virgin-plus/2024-02-10.pdf"]
    assert skips(owner_conn) == [
        ("mbna", "unreadable", 1),
        ("vancity", "unreadable", 1),
    ]
    assert "private-sub" not in out.model_dump_json()


def test_the_skip_counts_are_rewritten_on_every_walk(
    owner_conn: Connection,
    rolled_back_context: MakeContext,
    archive: Path,
    load_identifiers: Callable[[], None],
) -> None:
    load_identifiers()
    skipped = write_file(archive, "vancity/statement-9991234.pdf", b"%PDF renamed")
    file_op(rolled_back_context, archive)
    assert skips(owner_conn) == [("vancity", "identifier_in_path", 1)]

    skipped.rename(archive / "vancity/2024-01-31.pdf")
    out = file_op(rolled_back_context, archive)

    assert skips(owner_conn) == []
    assert out.registered == 1
    assert [row[0] for row in documents(owner_conn)] == ["vancity/2024-01-31.pdf"]


def test_a_path_stored_before_its_identifier_was_registered_is_withheld(
    owner_conn: Connection,
    rolled_back_context: MakeContext,
    archive: Path,
    load_identifiers: Callable[[], None],
) -> None:
    statement = write_file(archive, "vancity/statement-9991234.pdf", b"%PDF early")
    write_file(archive, "invoices-received/vendor-999-1234/inv.pdf", b"%PDF invoice")
    write_file(archive, "mbna/2024-01-31.pdf", b"%PDF untouched")
    assert file_op(rolled_back_context, archive).registered == 3

    load_identifiers()
    out = file_op(rolled_back_context, archive)

    assert (out.registered, out.known, out.withheld) == (0, 1, 2)
    assert documents(owner_conn) == [
        ("(withheld)", "invoices-received", "invoice", "pdf", "filed", "none"),
        ("(withheld)", "vancity", "statement", "pdf", "filed", "none"),
        ("mbna/2024-01-31.pdf", "mbna", "statement", "pdf", "filed", "none"),
    ]
    assert skips(owner_conn) == [
        ("invoices-received", "identifier_in_path", 1),
        ("vancity", "identifier_in_path", 1),
    ]
    stored = " ".join(
        f"{path} {issuer}"
        for path, issuer in owner_conn.execute(
            select(document.c.path, document.c.issuer)
        )
    )
    assert "1234" not in stored

    # Renamed, the file is adopted again: its row takes the new path back.
    statement.rename(archive / "vancity/2024-02-29.pdf")
    again = file_op(rolled_back_context, archive)
    assert (again.registered, again.moved, again.withheld) == (0, 1, 0)
    assert "vancity/2024-02-29.pdf" in [row[0] for row in documents(owner_conn)]
    assert document_count(owner_conn) == 3


def test_files_the_walk_cannot_register_are_counted_by_mapped_directory_only(
    owner_conn: Connection, rolled_back_context: MakeContext, archive: Path
) -> None:
    target = write_file(archive, "vancity/2024-01-31.pdf", b"%PDF real")
    (archive / "vancity/link.pdf").symlink_to(target)
    (archive / "mbna").mkdir()
    (archive / "mbna/linked-dir").symlink_to(archive / "vancity")
    write_file(archive, "virgin-plus/.2024-02-10.pdf.icloud", b"placeholder")
    write_file(archive, "virgin-plus/.DS_Store", b"finder")
    write_file(archive, ".hidden/2024-01-31.pdf", b"%PDF hidden")
    write_file(archive, "private-folder-name/2024-01-31.pdf", b"%PDF unmapped")
    write_file(archive, "loose-at-root.pdf", b"%PDF loose")

    out = file_op(rolled_back_context, archive)

    assert out.registered == 1
    assert skips(owner_conn) == [
        ("(unmapped)", "unmapped_directory", 2),
        ("mbna", "not_a_regular_file", 1),
        ("vancity", "not_a_regular_file", 1),
        ("virgin-plus", "not_downloaded", 1),
    ]
    assert "private-folder-name" not in out.model_dump_json()


def test_a_missing_archive_is_refused_without_its_path(
    rolled_back_context: MakeContext, tmp_path: Path
) -> None:
    missing = tmp_path / "no-such-archive"
    with pytest.raises(DocumentRefused) as refused:
        file_op(rolled_back_context, missing)
    assert "finance.paths.archive" in str(refused.value)
    assert str(missing) not in str(refused.value)


def test_the_service_role_files_and_a_member_is_refused(
    owner_conn: Connection, rolled_back_context: MakeContext, archive: Path
) -> None:
    write_file(archive, "vancity/2024-01-31.pdf", b"%PDF one")
    assert file_op(rolled_back_context, archive, Role.SERVICE).registered == 1
    with pytest.raises(RoleRefused):
        file_op(rolled_back_context, archive, Role.MEMBER)
