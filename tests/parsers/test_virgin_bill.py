"""The Virgin Plus bill through ``parse_document`` (FR-3 ``bill_identity``, FR-7).

A synthetic bill (layout text, every value fake) is filed into a temporary archive
under ``virgin-plus/`` and parsed with ``parse_document``; the tests read the archived
file as text where the real run calls pdftotext. The identity: unpaid balance plus
current charges equals the total due.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import uuid
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import Connection, Table, func, select

from rheo_finance.context import UnitOfWork
from rheo_finance.documents.registry import document_id, file_archive
from rheo_finance.manual.accounts import ACCOUNTS_FILE, IDENTIFIERS_FILE
from rheo_finance.manual.headers import MANUAL_FILES
from rheo_finance.manual.stage2 import account_id, load_accounts
from rheo_finance.parsers.registry import (
    DOCUMENT_REJECTED,
    Parsed,
    parse_document,
)
from rheo_finance.storage.refs import format_ref
from rheo_finance.storage.schema import document, document_line, event, statement_period

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "synthetic"
BILL = FIXTURES / "virgin_bill.txt"
NOW = dt.datetime(2026, 2, 15, 12, 0, tzinfo=dt.UTC)
NUMBER = "999000123"  # the fake account number the fixture prints
TOTAL = "Total amount due   Please pay by* Feb 23, 2026   $126.30"
UNPAID = "   Unpaid balance   $45.10\n"


def read_as_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


class Setup:
    def __init__(self, conn: Connection, tmp_path: Path) -> None:
        self.conn = conn
        self.uow = UnitOfWork(conn)
        self.data = tmp_path / "data"
        self.archive = tmp_path / "archive"
        self.archive.mkdir()

    def register(self, number: str = NUMBER) -> None:
        manual = self.data / "manual"
        manual.mkdir(parents=True, exist_ok=True)
        columns = MANUAL_FILES[ACCOUNTS_FILE]
        cells = dict.fromkeys(columns, "")
        cells.update(
            slug="mobile", label="Mobile", institution="virgin plus",
            kind="bill_vendor", currencies="CAD", cadence="monthly",
        )  # fmt: skip
        (manual / ACCOUNTS_FILE).write_text(
            ",".join(columns) + "\n" + ",".join(cells[c] for c in columns) + "\n",
            encoding="utf-8",
        )
        (manual / IDENTIFIERS_FILE).write_text(
            ",".join(MANUAL_FILES[IDENTIFIERS_FILE]) + f"\nmobile,{number},number,,\n",
            encoding="utf-8",
        )
        load_accounts(self.uow, self.data)

    def file(self, text: str) -> uuid.UUID:
        target = self.archive / "virgin-plus" / "2026-02-02.pdf"
        target.parent.mkdir(parents=True, exist_ok=True)
        data = text.encode("utf-8")
        target.write_bytes(data)
        file_archive(self.uow, self.archive, self.data, NOW)
        return document_id(hashlib.sha256(data).hexdigest())

    def parse(self, doc: uuid.UUID) -> Parsed:
        return parse_document(self.uow, doc, self.archive, extract_pdf=read_as_text)

    def count(self, table: Table, doc: uuid.UUID) -> int:
        query = select(func.count()).select_from(table)
        return int(self.conn.scalar(query.where(table.c.document_id == doc)) or 0)

    def period(self, doc: uuid.UUID) -> dict[str, object]:
        row = self.conn.execute(
            select(statement_period).where(statement_period.c.document_id == doc)
        ).one()
        return dict(row._mapping)

    def document_row(self, doc: uuid.UUID) -> dict[str, object]:
        row = self.conn.execute(select(document).where(document.c.id == doc)).one()
        return dict(row._mapping)

    def rejected_reasons(self, doc: uuid.UUID) -> list[object]:
        ref = format_ref("finance", "document", doc)
        return [
            data["reason"]
            for data in self.conn.scalars(
                select(event.c.data).where(
                    event.c.type == DOCUMENT_REJECTED, event.c.subject_ref == ref
                )
            )
        ]


@pytest.fixture
def setup(owner_conn: Connection, tmp_path: Path) -> Setup:
    return Setup(owner_conn, tmp_path)


def bill_text() -> str:
    text = BILL.read_text(encoding="utf-8")
    assert text.count(TOTAL) == 1 and text.count(UNPAID) == 1
    return text


def test_a_bill_whose_identity_holds_loads_one_tied_out_period(setup: Setup) -> None:
    setup.register()
    doc = setup.file(bill_text())

    assert setup.parse(doc) == Parsed("parsed", None, periods=1, lines=0)

    period = setup.period(doc)
    assert period["account_id"] == account_id("mobile")
    assert (period["control_kind"], period["tied_out"]) == ("bill_identity", True)
    assert (period["balance_due"], period["due_date"]) == (
        Decimal("126.30"), dt.date(2026, 2, 23),
    )  # fmt: skip
    # The bill prints no period: it covers its bill-date month.
    assert (period["period_start"], period["period_end"]) == (
        dt.date(2026, 2, 1), dt.date(2026, 2, 28),
    )  # fmt: skip
    assert period["currency"] == "CAD"
    assert setup.count(document_line, doc) == 0, "a bill has no ledger lines"
    row = setup.document_row(doc)
    assert (row["status"], row["parser_id"], row["extraction"]) == (
        "parsed", "virgin_plus.bill", "fixed_rule",
    )  # fmt: skip
    assert (row["period_start"], row["period_end"]) == (
        dt.date(2026, 2, 1), dt.date(2026, 2, 28),
    )  # fmt: skip


def test_a_bill_whose_identity_fails_is_rejected_with_zero_lines(setup: Setup) -> None:
    setup.register()
    doc = setup.file(bill_text().replace(TOTAL, TOTAL.replace("126.30", "126.31")))

    assert setup.parse(doc) == Parsed("rejected", "identity_mismatch")

    assert setup.count(statement_period, doc) == 0
    assert setup.count(document_line, doc) == 0
    row = setup.document_row(doc)
    assert (row["status"], row["reject_reason"]) == ("rejected", "identity_mismatch")
    assert (row["period_start"], row["period_end"]) == (
        dt.date(2026, 2, 1), dt.date(2026, 2, 28),
    )  # fmt: skip
    assert setup.rejected_reasons(doc) == ["identity_mismatch"]


def test_a_bill_printing_no_identity_loads_as_printed(setup: Setup) -> None:
    """No unpaid balance line: nothing to check, so the printed total stands, and
    the period is not tied out."""
    setup.register()
    doc = setup.file(bill_text().replace(UNPAID, ""))

    assert setup.parse(doc) == Parsed("parsed", None, periods=1, lines=0)

    period = setup.period(doc)
    assert (period["control_kind"], period["tied_out"]) == ("bill_identity", False)
    assert period["balance_due"] == Decimal("126.30")
    assert setup.rejected_reasons(doc) == []


def test_a_bill_for_an_unregistered_account_is_rejected(setup: Setup) -> None:
    setup.register("999000456")
    doc = setup.file(bill_text())

    assert setup.parse(doc) == Parsed("rejected", "unknown_account")
    assert setup.count(statement_period, doc) == 0
    assert setup.rejected_reasons(doc) == ["unknown_account"]


def test_text_that_is_not_a_bill_is_unparsed(setup: Setup) -> None:
    setup.register()
    doc = setup.file("A PAGE OF TEXT THAT IS NOT A BILL\n" * 9)

    assert setup.parse(doc) == Parsed("unparsed", "not_recognised")
    assert setup.count(statement_period, doc) == 0
