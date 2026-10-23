"""``finance.status.get``, ``finance.coverage.get`` and ``finance.document.list`` under
the reader DSN (FR-48, FR-51, AC-49a, AC-52a).

Seams: the three operations dispatched as a member on the reader connection, over a
fixture ledger committed to a throwaway database (``scratch_database``: the reader
cannot see rows inside another connection's rolled-back transaction), and over the
empty ``finance_test``. The fixture ledger is built by the real archive walk and derive
from synthetic files: one parsed chequing export, one rejected copy naming an unknown
account, one unparsed file, one file whose name carries a registered identifier
(skipped), one file outside every mapped directory (skipped for another reason), an
account with no data, one source discrepancy, and monitor files in Downloads, one old
enough to be waiting. Every value is synthetic.
"""

from __future__ import annotations

import datetime as dt
import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import pytest
from pydantic import BaseModel
from sqlalchemy import create_engine, func, insert, select, text
from sqlalchemy.pool import NullPool

from conftest import DatabaseUnderTest
from derive.support import (
    CSV_IDENTIFIER,
    FIXTURES,
    NOW,
    STATEMENT_IDENTIFIER,
    Ledger,
)
from rheo_finance import catalogue, operations
from rheo_finance.clock import FakeClock
from rheo_finance.context import FinanceContext, FinancePaths
from rheo_finance.contracts import Role
from rheo_finance.manual.accounts import ACCOUNTS_FILE, IDENTIFIERS_FILE
from rheo_finance.operations import dispatch
from rheo_finance.reports import ops as report_ops
from rheo_finance.reports.ops import (
    CoverageMonth,
    CoverageOut,
    DocumentListOut,
    StatusOut,
)
from rheo_finance.settings import SINKS, parse_settings
from rheo_finance.sinks.ops import SinkState, StateOut
from rheo_finance.storage.ids import uuid7
from rheo_finance.storage.schema import (
    account,
    coverage,
    document_line,
    source_discrepancy,
)

MakeContext = Callable[[Role], FinanceContext]
CSV_FIXTURE = (FIXTURES / "vancity_chequing_jump.csv").read_text(encoding="utf-8")
SPARE_IDENTIFIER = "999000000088"
UNKNOWN_IDENTIFIER = "999000000055-999-99901-99902"
SKIPPED_NAME = f"acct-{STATEMENT_IDENTIFIER}.csv"
UNMAPPED_NAME = "unmapped-folder/loose.csv"
# The chequing account opens four months before the fixture's only month of lines.
OPENED = "2025-10-01"


@dataclass(frozen=True)
class FixtureLedger:
    database: DatabaseUnderTest
    data: Path
    archive: Path
    downloads: Path
    # Every string no reader output may carry: identifiers, descriptions, the
    # skipped files' names.
    secrets: tuple[str, ...]
    missing_months: int
    coverage_rows: frozenset[tuple[str, dt.date, str, str | None]]


def _build(database: DatabaseUnderTest, root: Path) -> FixtureLedger:
    engine = create_engine(database.owner_url, poolclass=NullPool)
    downloads = root / "downloads"
    downloads.mkdir()
    try:
        with engine.connect() as conn:
            transaction = conn.begin()
            base = FinanceContext(
                settings=parse_settings(""),
                clock=FakeClock(NOW),
                paths=FinancePaths(data=root / "data"),
                role=Role.OWNER,
                snapshot_provider=lambda _uow, _ref: None,
            )
            ledger = Ledger(conn, base, root)
            ledger.manual(
                ACCOUNTS_FILE,
                f"chequing,Chequing,Vancity,chequing,CAD,monthly,,{OPENED},,,,,,,",
                "spare,Spare,Vancity,chequing,CAD,monthly,,,,,,,,,",
            )
            ledger.manual(
                IDENTIFIERS_FILE,
                f"chequing,{CSV_IDENTIFIER},number,,",
                f"chequing,{STATEMENT_IDENTIFIER},number,,",
                f"spare,{SPARE_IDENTIFIER},number,,",
            )
            ledger.file("vancity/export.csv", CSV_FIXTURE)
            ledger.file(
                "vancity/other.csv",
                CSV_FIXTURE.replace(CSV_IDENTIFIER, UNKNOWN_IDENTIFIER),
            )
            ledger.file("vancity/notes.csv", "NOT AN EXPORT\n")
            ledger.file(f"vancity/{SKIPPED_NAME}", "SKIPPED BY NAME\n")
            ledger.file(UNMAPPED_NAME, "NOT IN A MAPPED DIRECTORY\n")
            ledger.derive()

            line = conn.execute(
                select(document_line.c.id, document_line.c.document_id)
                .where(document_line.c.account_id.is_not(None))
                .order_by(document_line.c.id)
                .limit(1)
            ).one()
            chequing = conn.scalar(
                select(account.c.id).where(account.c.slug == "chequing")
            )
            conn.execute(
                insert(source_discrepancy).values(
                    id=uuid7(NOW),
                    account_id=chequing,
                    document_id=line.document_id,
                    document_line_id=line.id,
                    kind="unmatched_in_statement_window",
                )
            )
            descriptions = conn.scalars(
                select(document_line.c.description_raw).where(
                    document_line.c.description_raw.is_not(None)
                )
            ).all()
            missing = conn.scalar(
                select(func.count())
                .select_from(coverage)
                .where(coverage.c.state == "missing")
            )
            rows = conn.execute(
                select(
                    account.c.slug,
                    coverage.c.month,
                    coverage.c.state,
                    coverage.c.reason_code,
                ).select_from(
                    coverage.join(account, coverage.c.account_id == account.c.id)
                )
            ).all()
            transaction.commit()
    finally:
        engine.dispose()

    old = (NOW - dt.timedelta(days=2)).timestamp()
    for name, mtime in (("VCTY_sample.pdf", old), ("VCTY_fresh.pdf", NOW.timestamp())):
        (downloads / name).write_text("SYNTHETIC\n", encoding="utf-8")
        os.utime(downloads / name, (mtime, mtime))
    (downloads / "other.pdf").write_text("SYNTHETIC\n", encoding="utf-8")
    os.utime(downloads / "other.pdf", (old, old))

    # Each description's words of six letters or more: long enough not to occur in a
    # status line, a code or a digest by chance.
    words = sorted(
        {
            word
            for description in descriptions
            for word in description.split()
            if len(word) >= 6 and word.isalpha()
        }
    )
    return FixtureLedger(
        database=database,
        data=root / "data",
        archive=ledger.archive,
        downloads=downloads,
        secrets=(
            CSV_IDENTIFIER,
            STATEMENT_IDENTIFIER,
            SPARE_IDENTIFIER,
            UNKNOWN_IDENTIFIER,
            SKIPPED_NAME,
            "acct-",
            "loose.csv",
            "unmapped-folder",
            *words,
        ),  # fmt: skip
        missing_months=int(missing or 0),
        coverage_rows=frozenset(
            (slug, month, state, None if reason is None else reason.replace("_", " "))
            for slug, month, state, reason in rows
        ),
    )


@pytest.fixture(scope="module")
def fixture_ledger(
    scratch_database: DatabaseUnderTest, tmp_path_factory: pytest.TempPathFactory
) -> FixtureLedger:
    catalogue.load_all()
    return _build(scratch_database, tmp_path_factory.mktemp("status-ledger"))


def reader_context(ledger: FixtureLedger, role: Role = Role.MEMBER) -> FinanceContext:
    ctx = FinanceContext(
        settings=parse_settings(
            f'[finance.paths]\narchive = "{ledger.archive}"\n'
            f'downloads = "{ledger.downloads}"\n'
        ),
        clock=FakeClock(NOW),
        paths=FinancePaths(data=ledger.data),
        role=role,
        snapshot_provider=lambda _uow, _ref: None,
    )
    return operations.with_databases(
        ctx,
        owner_url=ledger.database.owner_url,
        reader_url=ledger.database.reader_url,
    )


def run(
    ledger: FixtureLedger, name: str, payload: dict[str, object] | None = None
) -> BaseModel:
    return dispatch(name, payload or {}, Role.MEMBER, ctx=reader_context(ledger))


def status_lines(ledger: FixtureLedger) -> dict[int, str]:
    out = run(ledger, "finance.status.get")
    assert isinstance(out, StatusOut)
    return {each.line_no: each.text for each in out.lines}


def coverage_out(ledger: FixtureLedger, **payload: object) -> CoverageOut:
    out = run(ledger, "finance.coverage.get", dict(payload))
    assert isinstance(out, CoverageOut)
    return out


def leaked(ledger: FixtureLedger, text_: str) -> list[str]:
    folded = text_.casefold()
    return [each for each in ledger.secrets if each.casefold() in folded]


@pytest.fixture
def sinks_off(sink_settings: Callable[..., None]) -> None:
    sink_settings()


# --- finance.status.get ---------------------------------------------------------------


def test_status_lines_on_the_fixture_ledger_under_the_reader(
    fixture_ledger: FixtureLedger, sinks_off: None
) -> None:
    lines = status_lines(fixture_ledger)

    assert list(lines) == [1, 2, 3, 4, 7, 9, 10]
    assert lines[1].startswith("as of 2026-02-20 ")
    assert lines[1].endswith(", database current")
    assert lines[2] == "last import: vancity 2026-02-20"
    assert lines[3] == "documents: 1 unparsed, 1 rejected"
    assert fixture_ledger.missing_months > 0
    assert lines[4] == (
        f"missing account-months: {fixture_ledger.missing_months} across 1 account"
    )
    assert lines[7] == "sinks off: " + " ".join(SINKS)
    # Only the identifier skip counts: the unmapped file is skipped for another reason.
    assert lines[9] == (
        "1 source discrepancy; 1 account with no data: Spare;"
        " 1 archive file skipped: file name carries an identifier, rename them"
        " (vancity 1)"
    )
    assert lines[10] == "1 monitor file waiting in Downloads"


def test_status_output_carries_no_identifier_description_or_skipped_path(
    fixture_ledger: FixtureLedger, sinks_off: None
) -> None:
    out = run(fixture_ledger, "finance.status.get")
    text_ = out.model_dump_json()
    assert leaked(fixture_ledger, text_) == []
    assert chr(0x2014) not in text_


def test_status_on_an_empty_ledger_prints_only_the_lines_that_always_apply(
    make_context: MakeContext, sinks_off: None
) -> None:
    out = dispatch("finance.status.get", {}, Role.MEMBER, ctx=make_context(Role.MEMBER))
    assert isinstance(out, StatusOut)
    lines = {each.line_no: each.text for each in out.lines}

    assert list(lines) == [1, 2, 3, 4, 7]
    assert lines[1].endswith(", database current")
    assert lines[2] == "last import: none"
    assert lines[3] == "documents: 0 unparsed, 0 rejected"
    assert lines[4] == "missing account-months: none"


def _sink(name: str, *, enabled: bool, approval: str) -> SinkState:
    return SinkState(
        sink=name,
        setting_enabled=enabled,
        approval=approval,  # type: ignore[arg-type]
        approved_at=None,
        approved_spec_digest=None,
        spec_digest=None,
        enabled=enabled and approval == "current",
    )


def test_status_line_7_names_disabled_sinks_and_stale_approvals(
    fixture_ledger: FixtureLedger, monkeypatch: pytest.MonkeyPatch
) -> None:
    def all_on(*_args: object) -> StateOut:
        return StateOut(
            sinks=[_sink(name, enabled=True, approval="current") for name in SINKS]
        )

    monkeypatch.setattr(report_ops, "sink_state", all_on)
    assert status_lines(fixture_ledger)[7] == "sinks: all enabled"

    def one_stale(*_args: object) -> StateOut:
        return StateOut(
            sinks=[
                _sink(name, enabled=True, approval="stale" if n == 0 else "current")
                for n, name in enumerate(SINKS)
            ]
        )

    monkeypatch.setattr(report_ops, "sink_state", one_stale)
    assert status_lines(fixture_ledger)[7] == (
        f"sinks off: {SINKS[0]}; approval stale: {SINKS[0]}"
    )


# --- finance.coverage.get -------------------------------------------------------------


def test_coverage_under_the_reader_is_the_matrix_with_reasons_in_words(
    fixture_ledger: FixtureLedger,
) -> None:
    out = coverage_out(fixture_ledger)

    got = {(m.account, m.month, m.state, m.reason) for m in out.months}
    assert got == fixture_ledger.coverage_rows
    assert {m.account_label for m in out.months} == {"Chequing"}
    assert out.no_data_accounts == 1
    assert sum(1 for m in out.months if m.state == "missing") == (
        fixture_ledger.missing_months
    )
    reasons = {m.reason for m in out.months if m.reason is not None}
    assert reasons and all("_" not in reason for reason in reasons)
    assert set(CoverageMonth.model_fields) == {
        "account", "account_label", "month", "state", "reason",
    }  # fmt: skip
    assert leaked(fixture_ledger, out.model_dump_json()) == []


def test_coverage_filters_by_account_and_year(fixture_ledger: FixtureLedger) -> None:
    every = coverage_out(fixture_ledger).months
    years = {m.month.year for m in every}
    assert years == {2025, 2026}

    assert coverage_out(fixture_ledger, account="spare").months == []
    assert coverage_out(fixture_ledger, account="chequing").months == every
    in_2025 = coverage_out(fixture_ledger, account="chequing", year=2025).months
    assert in_2025 and in_2025 == [m for m in every if m.month.year == 2025]


# --- finance.document.list ------------------------------------------------------------


def test_document_list_under_the_reader_never_shows_a_skipped_file(
    fixture_ledger: FixtureLedger,
) -> None:
    out = run(fixture_ledger, "finance.document.list")
    assert isinstance(out, DocumentListOut)

    assert [d.path for d in out.documents] == [
        "vancity/export.csv",
        "vancity/notes.csv",
        "vancity/other.csv",
    ]
    assert {d.path: d.status for d in out.documents} == {
        "vancity/export.csv": "parsed",
        "vancity/notes.csv": "unparsed",
        "vancity/other.csv": "rejected",
    }
    assert leaked(fixture_ledger, out.model_dump_json()) == []


# --- every report view, scanned whole -------------------------------------------------


def test_no_report_view_row_carries_an_identifier_description_or_skipped_path(
    fixture_ledger: FixtureLedger,
) -> None:
    engine = create_engine(fixture_ledger.database.reader_url, poolclass=NullPool)
    try:
        with engine.connect() as conn:
            names = conn.scalars(
                text(
                    "SELECT viewname FROM pg_views"
                    " WHERE schemaname = 'finance' AND viewname LIKE 'report%'"
                )
            ).all()
            assert "report_status" in names
            seen = 0
            for name in names:
                for row in conn.execute(text(f"SELECT * FROM finance.{name}")):
                    seen += 1
                    assert leaked(fixture_ledger, repr(tuple(row))) == [], name
    finally:
        engine.dispose()
    assert seen > 0
