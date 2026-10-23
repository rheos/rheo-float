"""``finance import``, ``rebuild``, ``digest``, ``coverage`` and ``status`` end to end
(FR-35, FR-51, AC-9 status part, AC-36, AC-52a).

Seams: ``cli.main.main`` with ``composition.build_context`` swapped for a context over
a throwaway database (``scratch_database``: the commands commit, and the reader must
see the rows), a fake clock and synthetic files in temporary directories, so no test
reads the owner's settings, data directory or database. ``finance import`` runs once
for the module over a Downloads directory holding one dateable chequing export and one
monitor file, and an archive holding an unparsed file and an image. ``coverage`` and
``status`` then run with an owner DSN that reaches nothing, which proves they need the
reader connection alone. The unreachable path points both DSNs at a local port nobody
listens on. Every value is synthetic.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import io
import json
import os
import socket
from collections.abc import Callable, Iterator
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass
from pathlib import Path

import pytest
from sqlalchemy.engine import URL

from conftest import DatabaseUnderTest
from derive.support import CSV_IDENTIFIER, FIXTURES, NOW
from rheo_finance import catalogue, composition, operations
from rheo_finance.cli.main import main
from rheo_finance.clock import FakeClock
from rheo_finance.context import FinanceContext, FinancePaths
from rheo_finance.contracts import Role
from rheo_finance.documents.registry import DocumentRefused
from rheo_finance.manual.accounts import ACCOUNTS_FILE, IDENTIFIERS_FILE
from rheo_finance.manual.headers import MANUAL_FILES
from rheo_finance.render import STATUS_MAX
from rheo_finance.settings import SINKS, parse_settings

EM = chr(0x2014)
CSV_FIXTURE = (FIXTURES / "vancity_chequing_jump.csv").read_text(encoding="utf-8")
STAMP = "as of 2026-02-20 05:00 MST"
# What no command may print: the identifier, the fixture's description words, its
# amounts and balances, and the Downloads file's own name.
SECRETS = (
    CSV_IDENTIFIER,
    "SAMPLE",
    "GROCER",
    "POS PURCHASE",
    "9.99",
    "489.51",
    "45.50",
    "bank-download",
)


@dataclass(frozen=True)
class Fixture:
    database: DatabaseUnderTest
    data: Path
    archive: Path
    downloads: Path
    import_out: str
    import_err: str
    import_code: int


@dataclass(frozen=True)
class Ran:
    code: int
    out: str
    err: str


def _context(fixture: Fixture, *, owner_url: URL, reader_url: URL) -> FinanceContext:
    ctx = FinanceContext(
        settings=parse_settings(
            f'[finance.paths]\narchive = "{fixture.archive}"\n'
            f'downloads = "{fixture.downloads}"\n'
        ),
        clock=FakeClock(NOW),
        paths=FinancePaths(data=fixture.data),
        role=Role.OWNER,
        snapshot_provider=lambda _uow, _ref: None,
    )
    return operations.with_databases(ctx, owner_url=owner_url, reader_url=reader_url)


def _nowhere() -> URL:
    """A DSN on a local port nobody listens on: connecting is refused at once."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        port = int(probe.getsockname()[1])
    return URL.create(
        "postgresql+psycopg",
        username="nobody",
        password="unused",
        host="127.0.0.1",
        port=port,
        database="finance_nowhere_test",
    )


def _write_manual(data: Path) -> None:
    folder = data / "manual"
    folder.mkdir(parents=True)
    rows = {
        ACCOUNTS_FILE: (
            # Months before December 2025 read "missing" with the reason in words.
            "chequing,Chequing,Vancity,chequing,CAD,monthly,,2025-10-01,,,,,,"
            "2025-12-01,mail_purged",
            "spare,Spare,Vancity,chequing,CAD,monthly,,,,,,,,,",
        ),
        IDENTIFIERS_FILE: (
            f"chequing,{CSV_IDENTIFIER},number,,",
            "spare,999000000088,number,,",
        ),
    }
    for name, lines in rows.items():
        (folder / name).write_text(
            ",".join(MANUAL_FILES[name]) + "\n" + "".join(f"{r}\n" for r in lines),
            encoding="utf-8",
        )


def _cli(
    fixture: Fixture,
    argv: list[str],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    *,
    owner_url: URL | None = None,
    reader_url: URL | None = None,
) -> Ran:
    ctx = _context(
        fixture,
        owner_url=owner_url or fixture.database.owner_url,
        reader_url=reader_url or fixture.database.reader_url,
    )
    monkeypatch.setattr(composition, "build_context", lambda _role: ctx)
    code = main(argv)
    captured = capsys.readouterr()
    return Ran(code, captured.out, captured.err)


def _clean(text: str) -> None:
    assert EM not in text
    leaked = [each for each in SECRETS if each.casefold() in text.casefold()]
    assert leaked == []


@pytest.fixture(scope="module")
def fixture(
    scratch_database: DatabaseUnderTest, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[Fixture]:
    catalogue.load_all()
    root = tmp_path_factory.mktemp("cli-commands")
    data, archive, downloads = root / "data", root / "archive", root / "downloads"
    _write_manual(data)
    (archive / "vancity").mkdir(parents=True)
    (archive / "vancity" / "notes.csv").write_text("NOT AN EXPORT\n", encoding="utf-8")
    (archive / "vancity" / "photo.jpg").write_text(
        "SYNTHETIC IMAGE\n", encoding="utf-8"
    )
    downloads.mkdir()
    (downloads / "bank-download.csv").write_text(CSV_FIXTURE, encoding="utf-8")
    monitor = downloads / "VCTY_sample.pdf"
    monitor.write_text("SYNTHETIC\n", encoding="utf-8")
    old = (NOW - dt.timedelta(days=2)).timestamp()
    os.utime(monitor, (old, old))

    pending = Fixture(scratch_database, data, archive, downloads, "", "", -1)
    ctx = _context(
        pending,
        owner_url=scratch_database.owner_url,
        reader_url=scratch_database.reader_url,
    )
    out, err = io.StringIO(), io.StringIO()
    with pytest.MonkeyPatch.context() as patch:
        # Sinks read $FINANCE_DATA_DIR/settings.toml: none here, so every sink is off.
        patch.setenv("FINANCE_DATA_DIR", str(data))
        patch.setattr(composition, "build_context", lambda _role: ctx)
        with redirect_stdout(out), redirect_stderr(err):
            code = main(["import"])
    yield dataclasses.replace(
        pending, import_out=out.getvalue(), import_err=err.getvalue(), import_code=code
    )


@pytest.fixture
def run(
    fixture: Fixture,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> Callable[..., Ran]:
    """Run ``finance <argv>``; every sink setting false, the data directory a
    temporary one."""
    monkeypatch.setenv("FINANCE_DATA_DIR", str(fixture.data))

    def go(
        *argv: str, owner_url: URL | None = None, reader_url: URL | None = None
    ) -> Ran:
        return _cli(
            fixture,
            list(argv),
            capsys,
            monkeypatch,
            owner_url=owner_url,
            reader_url=reader_url,
        )

    return go


@pytest.fixture
def reader_only(run: Callable[..., Ran]) -> Callable[..., Ran]:
    """``run`` with an owner DSN that reaches nothing: only reader operations work."""
    nowhere = _nowhere()

    def go(*argv: str) -> Ran:
        return run(*argv, owner_url=nowhere)

    return go


# --- finance import -------------------------------------------------------------------


def test_import_files_downloads_registers_the_archive_and_derives(
    fixture: Fixture,
) -> None:
    assert fixture.import_code == 0, fixture.import_err
    assert fixture.import_err == ""
    lines = fixture.import_out.splitlines()
    # Two archive files and the one copy filed from Downloads.
    assert lines[:5] == [
        f"finance import: 3 new documents registered, 1 filed from Downloads, {STAMP}",
        "",
        "Downloads: 1 filed, 0 already known, 0 refused by the path check, 0 not dated,"
        " 0 unreadable; 1 monitor files left to the monitors (1 waiting).",
        "Archive: 3 registered, 0 known, 0 moved, 0 duplicate copies, 0 paths withheld,"
        " 0 files skipped.",
        "",
    ]
    # Then the derive's own report.
    assert lines[5].startswith("derive: 3 parsed now, 0 kept; ")
    assert lines[6] == "documents: 1 needs manual entry, 1 parsed, 1 unparsed"
    assert lines[7].startswith("account-months: ")
    assert lines[8] == ""
    # The filer names the copy from the export's last date, never its own name.
    assert sorted(p.name for p in (fixture.archive / "vancity").iterdir()) == [
        "2026-02-16.csv", "notes.csv", "photo.jpg",
    ]  # fmt: skip
    assert (fixture.downloads / "bank-download.csv").exists()
    _clean(fixture.import_out)


def test_a_failing_derive_still_leaves_the_filing_counts(
    run: Callable[..., Ran], monkeypatch: pytest.MonkeyPatch
) -> None:
    def refused(*_args: object) -> object:
        raise DocumentRefused("a test refusal")

    spec = operations.SPECS["finance.ledger.derive"]
    monkeypatch.setitem(
        operations.SPECS,
        "finance.ledger.derive",
        spec.model_copy(update={"handler": refused}),
    )

    ran = run("import")

    assert ran.code == 1
    assert ran.out.splitlines()[0] == (
        f"finance import: 0 new documents registered, 0 filed from Downloads, {STAMP}"
    )
    assert "Archive: 0 registered, 3 known," in ran.out
    assert "derive:" not in ran.out
    assert ran.err == "finance import: refused. a test refusal\n"


def test_a_second_import_registers_nothing_new(run: Callable[..., Ran]) -> None:
    again = run("import")
    assert again.code == 0, again.err
    first = again.out.splitlines()[0]
    assert first == (
        f"finance import: 0 new documents registered, 0 filed from Downloads, {STAMP}"
    )
    assert "Downloads: 0 filed, 1 already known," in again.out
    _clean(again.out)


# --- finance rebuild and finance digest ---------------------------------------------


def test_rebuild_keeps_the_digest_and_digest_prints_table_counts(
    run: Callable[..., Ran],
) -> None:
    before = run("digest")
    assert before.code == 0, before.err
    head = before.out.splitlines()
    assert head[0].startswith("finance digest: ")
    digest = head[0].removeprefix("finance digest: ")
    assert len(digest) == 64 and all(c in "0123456789abcdef" for c in digest)
    assert head[1].endswith(f" tables, {STAMP}")
    assert head[2] == ""
    assert any(line.startswith("txn: ") for line in head[3:])

    rebuilt = run("rebuild")
    assert rebuilt.code == 0, rebuilt.err
    lines = rebuilt.out.splitlines()
    assert lines[0] == (
        f"finance rebuild: every document parsed again, 0 new documents registered,"
        f" {STAMP}"
    )
    assert lines[1].startswith("derive: 3 parsed now, 0 kept; ")
    assert lines[2] == "documents: 1 needs manual entry, 1 parsed, 1 unparsed"

    after = run("digest")
    assert after.out == before.out
    for each in (before, rebuilt, after):
        _clean(each.out)


# --- finance status -------------------------------------------------------------------


def test_status_json_under_the_reader_carries_the_status_lines(
    reader_only: Callable[..., Ran],
) -> None:
    ran = reader_only("status", "--json")
    assert ran.code == 0, ran.err
    lines = {each["line_no"]: each["text"] for each in json.loads(ran.out)["lines"]}

    assert list(lines) == [1, 2, 3, 4, 7, 9, 10]
    assert lines[1] == f"{STAMP}, database current"
    assert lines[2] == "last import: vancity 2026-02-20"
    assert lines[3] == "documents: 1 unparsed, 0 rejected, 1 needs manual entry"
    assert lines[4].startswith("missing account-months: ")
    assert lines[7] == "sinks off: " + " ".join(SINKS)
    assert lines[9] == "1 account with no data: Spare"
    assert lines[10] == "1 monitor file waiting in Downloads"
    _clean(ran.out)


def test_status_prints_the_same_lines_as_one_fixed_list(
    reader_only: Callable[..., Ran],
) -> None:
    as_json = reader_only("status", "--json")
    texts = [each["text"] for each in json.loads(as_json.out)["lines"]]
    ran = reader_only("status")

    assert ran.code == 0, ran.err
    assert ran.err == ""
    # One block: no blank line after the six-line headline.
    assert ran.out.splitlines() == texts
    assert len(texts) <= STATUS_MAX
    _clean(ran.out)


# --- finance coverage -----------------------------------------------------------------


def test_coverage_json_under_the_reader_is_the_matrix_and_the_counts(
    reader_only: Callable[..., Ran],
) -> None:
    ran = reader_only("coverage", "--json")
    assert ran.code == 0, ran.err
    got = json.loads(ran.out)

    assert got["as_of"] == STAMP
    assert got["no_data_accounts"] == 1
    months = got["months"]
    assert {m["account"] for m in months} == {"chequing"}
    assert {m["month"][:4] for m in months} == {"2025", "2026"}
    by_state = got["months_by_state"]
    assert sum(by_state.values()) == len(months)
    for state, count in by_state.items():
        assert count == sum(1 for m in months if m["state"] == state)
    assert by_state["missing"] >= 1
    reasons = {m["reason"] for m in months if m["reason"] is not None}
    assert reasons and all("_" not in reason for reason in reasons)
    _clean(ran.out)


def test_coverage_prints_counts_then_a_matrix_with_reasons_in_words(
    reader_only: Callable[..., Ran],
) -> None:
    got = json.loads(reader_only("coverage", "--json").out)
    by_state = got["months_by_state"]
    ran = reader_only("coverage")

    assert ran.code == 0, ran.err
    lines = ran.out.splitlines()
    assert lines[:4] == [
        f"finance coverage: {by_state['have']} have, {by_state['partial']} partial,"
        f" {by_state['missing']} missing account-months, {STAMP}",
        f"{by_state['rejected']} rejected, {by_state['unparsed']} unparsed,"
        f" {by_state['not_applicable']} not applicable",
        "1 account with no data",
        "",
    ]
    assert lines[4].startswith("Months January to December, one letter each: ")
    letter = {"have": "H", "partial": "P", "missing": "M", "rejected": "R",
              "unparsed": "U", "not_applicable": "-"}  # fmt: skip
    for year in ("2025", "2026"):
        cells = ["."] * 12
        for m in got["months"]:
            if m["month"].startswith(year):
                cells[int(m["month"][5:7]) - 1] = letter[m["state"]]
        row = " ".join("".join(cells[q : q + 3]) for q in range(0, 12, 3))
        assert f"Chequing {year}: {row}" in lines
    # Consecutive months with the same state and reason read as one range.
    assert "  2025-10 to 2025-11 missing: mail purged" in lines
    for m in got["months"]:
        if m["reason"] is not None:
            month = m["month"][:7]
            state = m["state"].replace("_", " ")
            assert any(
                line.startswith(f"  {month}") or f" to {month} " in line
                for line in lines
                if line.endswith(f" {state}: {m['reason']}")
            ), month
    _clean(ran.out)


# --- the unreachable database -------------------------------------------------------


def test_status_prints_one_line_and_exits_0_when_postgres_is_down(
    run: Callable[..., Ran],
) -> None:
    nowhere = _nowhere()
    ran = run("status", owner_url=nowhere, reader_url=nowhere)
    assert (ran.code, ran.out, ran.err) == (0, "database unreachable\n", "")

    as_json = run("status", "--json", owner_url=nowhere, reader_url=nowhere)
    assert as_json.code == 0
    assert json.loads(as_json.out) == {
        "lines": [{"line_no": 1, "text": "database unreachable"}]
    }


@pytest.mark.parametrize(
    "argv",
    [["import"], ["rebuild"], ["digest"], ["coverage"], ["coverage", "--json"]],
)
def test_other_commands_exit_1_with_the_same_words_when_postgres_is_down(
    run: Callable[..., Ran], argv: list[str]
) -> None:
    nowhere = _nowhere()
    ran = run(*argv, owner_url=nowhere, reader_url=nowhere)
    assert ran.code == 1
    assert ran.out == ""
    assert ran.err == f"finance {argv[0]}: database unreachable\n"


def test_a_server_that_refuses_the_password_is_not_unreachable(
    fixture: Fixture, run: Callable[..., Ran]
) -> None:
    # The server answers, so this is a failure with its own words, not "unreachable".
    wrong = fixture.database.reader_url.set(password="not-the-password")
    ran = run("status", reader_url=wrong)
    assert ran.code == 1
    assert ran.out == ""
    assert "database unreachable" not in ran.err
    assert ran.err.startswith("finance status: failed. OperationalError")
    assert "not-the-password" not in ran.err
