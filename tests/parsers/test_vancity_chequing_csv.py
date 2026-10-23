"""The headerless Vancity chequing CSV through ``parse_document`` (FR-3
``running_balance``, FR-8, edge case 7).

The fixture lists six rows newest first, with rows missing between its 5 Feb and
12 Feb lines (the balance jumps there). Every value is fake; the first column is the
fake full account identifier, registered as the account's ``number`` identifier.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import uuid
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import Connection, select

from rheo_finance.context import UnitOfWork
from rheo_finance.documents.registry import document_id, file_archive
from rheo_finance.manual.accounts import ACCOUNTS_FILE, IDENTIFIERS_FILE
from rheo_finance.manual.headers import MANUAL_FILES
from rheo_finance.manual.stage2 import account_id, load_accounts
from rheo_finance.parsers import vancity_chequing_csv
from rheo_finance.parsers.base import BalanceGap, VerifiedSegment
from rheo_finance.parsers.registry import (
    DOCUMENT_REJECTED,
    Parsed,
    account_lookup,
    parse_document,
)
from rheo_finance.storage.schema import document, document_line, event, statement_period

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "synthetic"
JUMP = FIXTURES / "vancity_chequing_jump.csv"
NOW = dt.datetime(2026, 2, 20, 12, 0, tzinfo=dt.UTC)
IDENTIFIER = "999000000077-999-99901-99902"
D = dt.date


class Setup:
    def __init__(self, conn: Connection, tmp_path: Path) -> None:
        self.conn = conn
        self.uow = UnitOfWork(conn)
        self.data = tmp_path / "data"
        self.archive = tmp_path / "archive"
        self.archive.mkdir()

    def register(self, *identifiers: str) -> None:
        """One chequing account holding ``identifiers`` (the fixture's by default)."""
        manual = self.data / "manual"
        manual.mkdir(parents=True, exist_ok=True)
        columns = MANUAL_FILES[ACCOUNTS_FILE]
        cells = dict.fromkeys(columns, "")
        cells.update(
            slug="chequing", label="Chequing", institution="Vancity",
            kind="chequing", currencies="CAD", cadence="monthly",
        )  # fmt: skip
        (manual / ACCOUNTS_FILE).write_text(
            ",".join(columns) + "\n" + ",".join(cells[c] for c in columns) + "\n",
            encoding="utf-8",
        )
        rows = [
            f"chequing,{identifier},number,,"
            for identifier in identifiers or (IDENTIFIER,)
        ]
        (manual / IDENTIFIERS_FILE).write_text(
            "\n".join([",".join(MANUAL_FILES[IDENTIFIERS_FILE]), *rows]) + "\n",
            encoding="utf-8",
        )
        load_accounts(self.uow, self.data)

    def file(self, text: str, name: str = "transactions-2026-02.csv") -> uuid.UUID:
        target = self.archive / "vancity" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        data = text.encode("utf-8")
        target.write_bytes(data)
        file_archive(self.uow, self.archive, self.data, NOW)
        return document_id(hashlib.sha256(data).hexdigest())

    def parse(self, doc: uuid.UUID) -> Parsed:
        return parse_document(self.uow, doc, self.archive)

    def lines(self, doc: uuid.UUID) -> list[tuple[object, ...]]:
        rows = self.conn.execute(
            select(
                document_line.c.posting_date,
                document_line.c.amount,
                document_line.c.running_balance,
            )
            .where(document_line.c.document_id == doc)
            .order_by(document_line.c.line_no)
        ).all()
        return [tuple(row) for row in rows]

    def period(self, doc: uuid.UUID) -> dict[str, object]:
        row = self.conn.execute(
            select(statement_period).where(statement_period.c.document_id == doc)
        ).one()
        return dict(row._mapping)


@pytest.fixture
def setup(owner_conn: Connection, tmp_path: Path) -> Setup:
    return Setup(owner_conn, tmp_path)


def row(
    day: str, withdrawal: str, deposit: str, balance: str, identifier: str = IDENTIFIER
) -> str:
    """One export row: identifier, date, description, empty column, withdrawal,
    deposit, balance."""
    return f'{identifier},{day},"SAMPLE ITEM",,{withdrawal},{deposit},{balance}\n'


def jump_text() -> str:
    return JUMP.read_text(encoding="utf-8")


def after_the_jump() -> str:
    """The fixture's three newest rows: one continuous run."""
    return "".join(jump_text().splitlines(keepends=True)[:3])


def test_a_continuous_export_is_one_verified_segment(setup: Setup) -> None:
    setup.register()
    doc = setup.file(after_the_jump())

    assert setup.parse(doc) == Parsed(
        "parsed", None, periods=1, lines=3, segments=1, gaps=0
    )

    # Oldest first, though the file lists newest first.
    assert setup.lines(doc) == [
        (D(2026, 2, 12), Decimal("-30.00"), Decimal("424.50")),
        (D(2026, 2, 12), Decimal("75.00"), Decimal("499.50")),
        (D(2026, 2, 16), Decimal("-9.99"), Decimal("489.51")),
    ]
    period = setup.period(doc)
    assert period["account_id"] == account_id("chequing")
    assert (period["control_kind"], period["tied_out"], period["currency"]) == (
        "running_balance", True, "CAD",
    )  # fmt: skip
    assert (period["period_start"], period["period_end"]) == (
        D(2026, 2, 12), D(2026, 2, 16),
    )  # fmt: skip
    row = setup.conn.execute(
        select(document.c.status, document.c.parser_id).where(document.c.id == doc)
    ).one()
    assert tuple(row) == ("parsed", "vancity.chequing_csv")


def test_a_balance_jump_loads_both_lines_and_returns_a_gap(setup: Setup) -> None:
    setup.register()
    doc = setup.file(jump_text())

    assert setup.parse(doc) == Parsed(
        "parsed", None, periods=1, lines=6, segments=2, gaps=1
    )

    lines = setup.lines(doc)
    assert len(lines) == 6
    assert lines[2] == (D(2026, 2, 5), Decimal("-45.50"), Decimal("554.50"))
    assert lines[3] == (D(2026, 2, 12), Decimal("-30.00"), Decimal("424.50"))
    period = setup.period(doc)
    assert (period["control_kind"], period["tied_out"]) == ("running_balance", False)
    assert (period["period_start"], period["period_end"]) == (
        D(2026, 2, 2), D(2026, 2, 16),
    )  # fmt: skip

    result = vancity_chequing_csv.parse(jump_text(), account_lookup(setup.uow))
    (registered,) = {period.account for period in result.periods}
    assert result.gaps == (BalanceGap(registered, D(2026, 2, 5), D(2026, 2, 12)),)
    assert result.segments == (
        VerifiedSegment(registered, D(2026, 2, 2), D(2026, 2, 5)),
        VerifiedSegment(registered, D(2026, 2, 12), D(2026, 2, 16)),
    )


def test_the_account_identifier_is_stored_nowhere(setup: Setup) -> None:
    setup.register()
    doc = setup.file(jump_text())
    setup.parse(doc)

    digits = IDENTIFIER.replace("-", "")
    for table in (document_line, statement_period, document, event):
        for row in setup.conn.execute(select(table)):
            for value in row:
                text = str(value)
                assert IDENTIFIER not in text and digits not in text, table.name


def test_an_unregistered_identifier_is_rejected(setup: Setup) -> None:
    setup.register("999000000088-999-99901-99902")
    doc = setup.file(jump_text())

    assert setup.parse(doc) == Parsed("rejected", "unknown_account")

    assert setup.lines(doc) == []
    reasons = [
        data["reason"]
        for data in setup.conn.scalars(
            select(event.c.data).where(event.c.type == DOCUMENT_REJECTED)
        )
    ]
    assert reasons == ["unknown_account"]


def test_a_csv_of_another_layout_is_unparsed(setup: Setup) -> None:
    setup.register()
    header = "Account,Date,Description,Cheque,Withdrawal,Deposit,Balance\n"
    doc = setup.file(header + after_the_jump())

    assert setup.parse(doc) == Parsed("unparsed", "not_recognised")
    assert setup.lines(doc) == []


def test_a_lone_line_between_two_breaks_verifies_nothing(setup: Setup) -> None:
    setup.register()
    text = (
        row("01-Feb-2026", "", "100.00", "100.00")
        + row("02-Feb-2026", "10.00", "", "90.00")  # continues the first
        + row("04-Feb-2026", "5.00", "", "50.00")  # a break before and after
        + row("06-Feb-2026", "5.00", "", "20.00")
    )
    doc = setup.file(text)

    assert setup.parse(doc) == Parsed(
        "parsed", None, periods=1, lines=4, segments=1, gaps=2
    )
    result = vancity_chequing_csv.parse(text, account_lookup(setup.uow))
    (registered,) = {period.account for period in result.periods}
    assert result.segments == (
        VerifiedSegment(registered, D(2026, 2, 1), D(2026, 2, 2)),
    )
    assert result.gaps == (
        BalanceGap(registered, D(2026, 2, 2), D(2026, 2, 4)),
        BalanceGap(registered, D(2026, 2, 4), D(2026, 2, 6)),
    )
    assert setup.period(doc)["tied_out"] is False


def test_a_one_row_file_loads_its_line_and_verifies_nothing(setup: Setup) -> None:
    setup.register()
    doc = setup.file(row("03-Feb-2026", "12.00", "", "88.00"))

    assert setup.parse(doc) == Parsed(
        "parsed", None, periods=1, lines=1, segments=0, gaps=0
    )
    assert setup.lines(doc) == [(D(2026, 2, 3), Decimal("-12.00"), Decimal("88.00"))]
    period = setup.period(doc)
    assert (period["period_start"], period["period_end"]) == (
        D(2026, 2, 3), D(2026, 2, 3),
    )  # fmt: skip
    assert period["tied_out"] is False


def test_two_identifiers_of_one_account_in_one_file_are_ambiguous(
    setup: Setup,
) -> None:
    other = "999000000066-999-99901-99903"
    setup.register(IDENTIFIER, other)
    doc = setup.file(
        row("02-Feb-2026", "", "100.00", "100.00")
        + row("03-Feb-2026", "10.00", "", "90.00", identifier=other)
    )

    assert setup.parse(doc) == Parsed("rejected", "ambiguous_account")
    assert setup.lines(doc) == []


def test_a_row_with_a_withdrawal_and_a_deposit_becomes_two_lines(
    setup: Setup,
) -> None:
    """The withdrawal first, carrying the balance between them; the run stays
    continuous."""
    setup.register()
    doc = setup.file(
        row("02-Feb-2026", "", "100.00", "100.00")
        + row("03-Feb-2026", "20.00", "50.00", "130.00")
    )

    assert setup.parse(doc) == Parsed(
        "parsed", None, periods=1, lines=3, segments=1, gaps=0
    )
    assert setup.lines(doc) == [
        (D(2026, 2, 2), Decimal("100.00"), Decimal("100.00")),
        (D(2026, 2, 3), Decimal("-20.00"), Decimal("80.00")),
        (D(2026, 2, 3), Decimal("50.00"), Decimal("130.00")),
    ]


def test_an_oldest_first_file_gives_the_same_lines(setup: Setup) -> None:
    setup.register()
    newest_first = setup.file(jump_text())
    oldest_first = setup.file(
        "".join(reversed(jump_text().splitlines(keepends=True))), "oldest-first.csv"
    )

    expected = Parsed("parsed", None, periods=1, lines=6, segments=2, gaps=1)
    assert setup.parse(newest_first) == expected
    assert setup.parse(oldest_first) == expected
    assert setup.lines(oldest_first) == setup.lines(newest_first)


def test_the_registered_identifier_matches_whatever_its_separators(
    setup: Setup,
) -> None:
    setup.register(IDENTIFIER.replace("-", " "))
    doc = setup.file(jump_text())

    assert setup.parse(doc).status == "parsed"
    assert setup.period(doc)["account_id"] == account_id("chequing")
