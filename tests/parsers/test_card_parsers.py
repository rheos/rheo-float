"""The card parsers through ``parse_document`` (FR-3, FR-8, FR-14, FR-19, FR-20;
AC-8 parser part): the Vancity Visa statement, headerless CSV export and transaction
download, and the MBNA statement.

Seam: ``parse_document`` over a synthetic document filed into a temporary archive,
against accounts registered through the manual files. The statements tie out and
reject on a one-cent difference; the card fields are populated (a statement that
prints no minimum gives a null one); a foreign purchase keeps the issuer's CAD amount;
the cardholder's name never reaches a stored row. Every value is fake; the fixtures
are layout text, read as text where the real run calls pdftotext.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import hashlib
import uuid
from decimal import Decimal
from pathlib import Path
from typing import Any

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
    parsers_for,
)
from rheo_finance.storage.refs import format_ref
from rheo_finance.storage.schema import (
    document,
    document_line,
    event,
    rejected_period,
    statement_period,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "synthetic"
NOW = dt.datetime(2026, 3, 15, 12, 0, tzinfo=dt.UTC)
# The invented cardholder every fixture prints; no stored row may carry it.
CARDHOLDER = "sample cardholder"


@dataclasses.dataclass(frozen=True)
class Card:
    slug: str
    institution: str
    identifier: str


VISA = Card("visa", "Vancity", "9999 99** **** 9991")
MBNA = Card("mbna-card", "MBNA", "9999 99XX XXXX 9992")
OTHER_VISA = Card("other-visa", "Vancity", "9999 99** **** 9993")


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def read_as_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


class Setup:
    def __init__(self, conn: Connection, tmp_path: Path) -> None:
        self.conn = conn
        self.uow = UnitOfWork(conn)
        self.data = tmp_path / "data"
        self.archive = tmp_path / "archive"
        self.archive.mkdir()

    def register(self, *cards: Card) -> None:
        manual = self.data / "manual"
        manual.mkdir(parents=True, exist_ok=True)
        columns = MANUAL_FILES[ACCOUNTS_FILE]
        rows = [",".join(columns)]
        for card in cards:
            cells = dict.fromkeys(columns, "")
            cells.update(
                slug=card.slug,
                label=card.slug.title(),
                institution=card.institution,
                kind="credit_card",
                currencies="CAD",
                cadence="monthly",
            )
            rows.append(",".join(cells[column] for column in columns))
        (manual / ACCOUNTS_FILE).write_text("\n".join(rows) + "\n", encoding="utf-8")
        identifiers = [",".join(MANUAL_FILES[IDENTIFIERS_FILE])]
        identifiers += [
            f"{card.slug},{card.identifier},masked_card,," for card in cards
        ]
        (manual / IDENTIFIERS_FILE).write_text(
            "\n".join(identifiers) + "\n", encoding="utf-8"
        )
        load_accounts(self.uow, self.data)

    def file(self, relative: str, text: str) -> uuid.UUID:
        target = self.archive / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        data = text.encode("utf-8")
        target.write_bytes(data)
        file_archive(self.uow, self.archive, self.data, NOW)
        return document_id(hashlib.sha256(data).hexdigest())

    def parse(self, doc: uuid.UUID) -> Parsed:
        return parse_document(self.uow, doc, self.archive, extract_pdf=read_as_text)

    def period(self, doc: uuid.UUID) -> Any:
        return self.conn.execute(
            select(statement_period).where(statement_period.c.document_id == doc)
        ).one()

    def lines(self, doc: uuid.UUID) -> list[Any]:
        return list(
            self.conn.execute(
                select(document_line)
                .where(document_line.c.document_id == doc)
                .order_by(document_line.c.line_no)
            ).all()
        )

    def count(self, table: Table, doc: uuid.UUID) -> int:
        query = select(func.count()).select_from(table)
        return int(self.conn.scalar(query.where(table.c.document_id == doc)) or 0)

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

    def stored_text(self, doc: uuid.UUID) -> list[str]:
        """Every text value stored for the document: its row, its periods, its lines
        and its events, casefolded."""
        found: list[str] = []
        for table in (document, statement_period, document_line):
            key = table.c.id if table is document else table.c.document_id
            for row in self.conn.execute(select(table).where(key == doc)):
                found += [str(value).casefold() for value in row if value is not None]
        ref = format_ref("finance", "document", doc)
        for data in self.conn.scalars(
            select(event.c.data).where(event.c.subject_ref == ref)
        ):
            found.append(str(data).casefold())
        return found


@pytest.fixture
def setup(owner_conn: Connection, tmp_path: Path) -> Setup:
    return Setup(owner_conn, tmp_path)


def amounts(lines: list[Any]) -> list[Decimal]:
    return [line.amount for line in lines]


# --- the Vancity Visa statement ----------------------------------------------------


VISA_STATEMENT = "vancity-visa/2026-01-05.pdf"


def test_the_visa_statement_ties_out_with_its_card_fields(setup: Setup) -> None:
    setup.register(VISA)
    doc = setup.file(VISA_STATEMENT, fixture("vancity_visa_statement.txt"))

    assert setup.parse(doc) == Parsed("parsed", None, periods=1, lines=7)

    period = setup.period(doc)
    assert period.account_id == account_id("visa")
    # The period crosses the new year; its end is the statement date.
    assert (period.period_start, period.period_end) == (
        dt.date(2025, 12, 6), dt.date(2026, 1, 5),
    )  # fmt: skip
    assert (period.currency, period.control_kind, period.tied_out) == (
        "CAD", "summary", True,
    )  # fmt: skip
    # The balance owed, negated: the card's lines run from one to the other.
    assert (period.opening, period.closing) == (
        Decimal("-1250.00"),
        Decimal("-1207.27"),
    )
    assert (period.total_debits, period.total_credits) == (
        Decimal("269.77"), Decimal("312.50"),
    )  # fmt: skip
    assert period.balance_due == Decimal("1207.27")
    assert period.minimum_due == Decimal("36.22")
    assert period.due_date == dt.date(2026, 1, 30)
    # The payment line, not the refund the summary folds into "Payments & credits".
    assert period.total_payments == Decimal("300.00")


def test_the_visa_statement_lines_dates_signs_and_hints(setup: Setup) -> None:
    setup.register(VISA)
    doc = setup.file(VISA_STATEMENT, fixture("vancity_visa_statement.txt"))
    setup.parse(doc)

    lines = setup.lines(doc)
    assert [(line.txn_date, line.posting_date) for line in lines] == [
        (dt.date(2025, 12, 5), dt.date(2025, 12, 8)),
        (dt.date(2025, 12, 10), dt.date(2025, 12, 11)),
        (dt.date(2025, 12, 15), dt.date(2025, 12, 15)),
        (dt.date(2025, 12, 18), dt.date(2025, 12, 19)),
        (dt.date(2025, 12, 22), dt.date(2025, 12, 23)),
        (dt.date(2026, 1, 2), dt.date(2026, 1, 2)),
        (dt.date(2026, 1, 5), dt.date(2026, 1, 5)),
    ]
    assert amounts(lines) == [
        Decimal(each)
        for each in (
            "-45.20",
            "-27.15",
            "300.00",
            "12.50",
            "-140.00",
            "-39.00",
            "-18.42",
        )
    ]
    # The right-hand column's text is not part of a description.
    assert lines[1].description_raw == "SAMPLE CLOUD HOSTING SAMPLE.TEST CA"
    assert lines[2].description_raw == "PAYMENT RECEIVED -- THANK YOU"
    assert {line.currency for line in lines} == {"CAD"}
    assert {line.line_kind for line in lines} == {"transaction"}

    payment = lines[2]
    assert (payment.transfer_hint, payment.hint_account_slug) == (True, "vancity")
    assert [line.fee_type for line in lines] == [
        None, None, None, None, None, "annual_fee", "interest",
    ]  # fmt: skip
    assert [line.transfer_hint for line in lines].count(True) == 1
    assert not any(line.reversal_hint or line.etransfer_hint for line in lines)


def test_a_foreign_purchase_keeps_the_issuers_cad_amount(setup: Setup) -> None:
    setup.register(VISA)
    doc = setup.file(VISA_STATEMENT, fixture("vancity_visa_statement.txt"))
    setup.parse(doc)

    foreign = [
        (line.amount, line.currency, line.original_amount, line.original_currency,
         line.stated_cad)
        for line in setup.lines(doc)
        if line.original_currency is not None
    ]  # fmt: skip
    # 19.99 USD at the printed rate is not exactly 27.15: the stated CAD stands.
    assert foreign == [
        (Decimal("-27.15"), "CAD", Decimal("-19.99"), "USD", Decimal("-27.15")),
        (Decimal("-140.00"), "CAD", Decimal("-100.00"), "USD", Decimal("-140.00")),
    ]


@pytest.mark.parametrize(
    ("printed", "changed"),
    [
        ("$1,207.27", "$1,207.28"),  # the statement balance, everywhere it prints
        (
            "SAMPLE GROCER SAMPLE CITY BC   $45.20",
            "SAMPLE GROCER SAMPLE CITY BC   $45.21",
        ),
        ("Interest   +   $18.42", "Interest   +   $18.43"),
    ],
)
def test_a_visa_statement_a_cent_off_is_rejected_with_zero_lines(
    setup: Setup, printed: str, changed: str
) -> None:
    setup.register(VISA)
    text = fixture("vancity_visa_statement.txt")
    assert printed in text
    doc = setup.file(VISA_STATEMENT, text.replace(printed, changed))

    assert setup.parse(doc) == Parsed("rejected", "summary_mismatch")

    assert setup.count(document_line, doc) == 0
    assert setup.count(statement_period, doc) == 0
    assert setup.count(rejected_period, doc) == 1
    assert setup.rejected_reasons(doc) == ["summary_mismatch"]


def test_a_visa_statement_with_no_printed_minimum_has_a_null_minimum(
    setup: Setup,
) -> None:
    setup.register(VISA)
    doc = setup.file(
        "vancity-visa/2026-03-05.pdf", fixture("vancity_visa_statement_no_minimum.txt")
    )

    assert setup.parse(doc) == Parsed("parsed", None, periods=1, lines=2)

    period = setup.period(doc)
    assert period.minimum_due is None
    assert (period.balance_due, period.due_date, period.total_payments) == (
        Decimal("25.00"), dt.date(2026, 3, 30), Decimal("400.00"),
    )  # fmt: skip
    assert (period.period_end, period.tied_out) == (dt.date(2026, 3, 5), True)


def test_a_visa_statement_for_an_unregistered_card_is_rejected(setup: Setup) -> None:
    setup.register(OTHER_VISA)
    doc = setup.file(VISA_STATEMENT, fixture("vancity_visa_statement.txt"))

    assert setup.parse(doc) == Parsed("rejected", "unknown_account")
    assert setup.count(document_line, doc) == 0


def test_text_no_visa_parser_reads_is_unparsed(setup: Setup) -> None:
    setup.register(VISA)
    doc = setup.file(VISA_STATEMENT, "A PAGE OF TEXT THAT IS NOT A STATEMENT\n" * 9)

    assert setup.parse(doc) == Parsed("unparsed", "not_recognised")


# --- the Vancity Visa CSV export -----------------------------------------------------


def test_the_visa_export_loads_oldest_first_with_control_none(setup: Setup) -> None:
    setup.register(VISA)
    doc = setup.file("vancity-visa/2024-02-21.csv", fixture("vancity_visa_export.csv"))

    assert setup.parse(doc) == Parsed("parsed", None, periods=1, lines=5)

    period = setup.period(doc)
    assert (period.control_kind, period.tied_out, period.account_id) == (
        "none", False, account_id("visa"),
    )  # fmt: skip
    assert (period.period_start, period.period_end) == (
        dt.date(2024, 2, 6), dt.date(2024, 2, 21),
    )  # fmt: skip
    assert period.opening is None and period.minimum_due is None

    lines = setup.lines(doc)
    assert [(line.txn_date, line.posting_date) for line in lines] == [
        (dt.date(2024, 2, 6), dt.date(2024, 2, 6)),
        (dt.date(2024, 2, 8), dt.date(2024, 2, 9)),
        (dt.date(2024, 2, 10), dt.date(2024, 2, 12)),
        (dt.date(2024, 2, 15), dt.date(2024, 2, 15)),
        (dt.date(2024, 2, 20), dt.date(2024, 2, 21)),
    ]
    # A debit is spending, a credit a payment or a refund.
    assert amounts(lines) == [
        Decimal(each) for each in ("-18.42", "-45.20", "12.50", "250.00", "-6.75")
    ]
    assert [line.transfer_hint for line in lines] == [False, False, False, True, False]
    assert lines[3].hint_account_slug == "vancity"
    assert lines[0].fee_type == "interest"


def test_the_visa_export_never_stores_the_cardholders_name(setup: Setup) -> None:
    setup.register(VISA)
    text = fixture("vancity_visa_export.csv")
    assert text.casefold().count(CARDHOLDER) == 5
    doc = setup.file("vancity-visa/2024-02-21.csv", text)
    assert setup.parse(doc).status == "parsed"

    assert all(line.counterparty is None for line in setup.lines(doc))
    stored = setup.stored_text(doc)
    assert stored
    assert not any(CARDHOLDER in value for value in stored)


def test_a_visa_export_for_an_unregistered_card_is_rejected(setup: Setup) -> None:
    setup.register(OTHER_VISA)
    doc = setup.file("vancity-visa/2024-02-21.csv", fixture("vancity_visa_export.csv"))

    assert setup.parse(doc) == Parsed("rejected", "unknown_account")
    assert setup.count(document_line, doc) == 0


@pytest.mark.parametrize(
    ("printed", "changed"),
    [
        (",CAD,6.75,\n", ",CAD,6.75,1.00\n"),  # both a debit and a credit
        (",CAD,6.75,\n", ",USD,6.75,\n"),  # a currency the export does not post
        (",CAD,6.75,\n", ",CAD,6.75\n"),  # seven columns
    ],
)
def test_a_csv_that_is_not_the_visa_export_is_unparsed(
    setup: Setup, printed: str, changed: str
) -> None:
    setup.register(VISA)
    text = fixture("vancity_visa_export.csv")
    assert printed in text
    doc = setup.file("vancity-visa/2024-02-21.csv", text.replace(printed, changed))

    assert setup.parse(doc) == Parsed("unparsed", "not_recognised")


# --- the Vancity Visa transaction download -------------------------------------------


VISA_DOWNLOAD = "vancity-visa/2025-10-20-download.pdf"


def test_the_visa_download_loads_with_control_none(setup: Setup) -> None:
    setup.register(VISA)
    doc = setup.file(VISA_DOWNLOAD, fixture("vancity_visa_transactions.txt"))

    assert setup.parse(doc) == Parsed("parsed", None, periods=1, lines=5)

    row = setup.conn.execute(
        select(document.c.parser_id, document.c.parser_version).where(
            document.c.id == doc
        )
    ).one()
    assert tuple(row) == ("vancity_visa.transactions", "transactions-1")
    period = setup.period(doc)
    assert (period.control_kind, period.tied_out) == ("none", False)
    assert (period.period_start, period.period_end) == (
        dt.date(2025, 9, 6), dt.date(2025, 10, 20),
    )  # fmt: skip

    lines = setup.lines(doc)
    assert [line.posting_date for line in lines] == [
        dt.date(2025, 9, 11), dt.date(2025, 9, 29), dt.date(2025, 10, 3),
        dt.date(2025, 10, 15), dt.date(2025, 10, 20),
    ]  # fmt: skip
    assert amounts(lines) == [
        Decimal(each) for each in ("-29.00", "-45.20", "12.50", "300.00", "-140.00")
    ]
    # A description runs on to the next line; the page footer is not part of one.
    assert [line.description_raw for line in lines] == [
        "OVERLIMIT FEE",
        "SAMPLE GROCER SAMPLE CITY BC 99902",
        "SAMPLE BOOKSHOP SAMPLE CITY BC",
        "PAYMENT RECEIVED -- THANK YOU",
        "SAMPLE AI SUBSCRIPTION SAMPLE.TEST CA 99901",
    ]
    assert lines[3].transfer_hint and lines[3].hint_account_slug == "vancity"
    assert lines[0].fee_type == "overlimit_fee"
    stored = setup.stored_text(doc)
    assert not any(CARDHOLDER in value for value in stored)


def test_the_older_unsigned_download_is_unparsed(setup: Setup) -> None:
    """The older download prints no sign: a refund would read as a purchase."""
    setup.register(VISA)
    text = (
        fixture("vancity_visa_transactions.txt")
        .replace("999999XXXXXX9991", "9999 99•• •••• 9991")
        .replace("-$", "$")
    )
    doc = setup.file(VISA_DOWNLOAD, text)

    assert setup.parse(doc) == Parsed("unparsed", "not_recognised")


# --- the MBNA statement --------------------------------------------------------------


MBNA_STATEMENT = "mbna/2025-09-20.pdf"


def test_the_mbna_statement_ties_out_with_its_card_fields(setup: Setup) -> None:
    setup.register(MBNA)
    doc = setup.file(MBNA_STATEMENT, fixture("mbna_statement.txt"))

    assert setup.parse(doc) == Parsed("parsed", None, periods=1, lines=5)

    period = setup.period(doc)
    assert period.account_id == account_id("mbna-card")
    assert (period.period_start, period.period_end) == (
        dt.date(2025, 8, 21), dt.date(2025, 9, 20),
    )  # fmt: skip
    assert (period.control_kind, period.tied_out) == ("summary", True)
    assert (period.opening, period.closing) == (
        Decimal("-1000.00"),
        Decimal("-1086.17"),
    )
    assert period.balance_due == Decimal("1086.17")
    assert period.minimum_due == Decimal("32.00")
    assert period.due_date == dt.date(2025, 10, 14)
    assert period.total_payments == Decimal("150.00")

    lines = setup.lines(doc)
    assert amounts(lines) == [
        Decimal(each) for each in ("150.00", "-9.99", "-215.68", "20.00", "-30.50")
    ]
    assert [(line.txn_date, line.posting_date) for line in lines][:2] == [
        (dt.date(2025, 9, 2), dt.date(2025, 9, 2)),
        (dt.date(2025, 8, 25), dt.date(2025, 8, 26)),
    ]
    # The reference number is not part of the description; the columns' gaps close.
    assert lines[1].description_raw == "SAMPLE STREAMING SAMPLE CITY ON"
    assert (lines[0].description_raw, lines[0].transfer_hint) == ("PAYMENT", True)
    assert lines[4].fee_type == "interest"
    assert [line.transfer_hint for line in lines].count(True) == 1
    assert not any(CARDHOLDER in value for value in setup.stored_text(doc))


@pytest.mark.parametrize(
    ("printed", "changed"),
    [
        ("9913   $215.68", "9913   $215.69"),
        ("Your New Balance   $1,086.17", "Your New Balance   $1,086.18"),
    ],
)
def test_an_mbna_statement_a_cent_off_is_rejected_with_zero_lines(
    setup: Setup, printed: str, changed: str
) -> None:
    setup.register(MBNA)
    text = fixture("mbna_statement.txt")
    assert text.count(printed) == 1
    doc = setup.file(MBNA_STATEMENT, text.replace(printed, changed))

    assert setup.parse(doc) == Parsed("rejected", "summary_mismatch")
    assert setup.count(document_line, doc) == 0
    assert setup.rejected_reasons(doc) == ["summary_mismatch"]


def test_an_mbna_statement_with_no_printed_minimum_has_a_null_minimum(
    setup: Setup,
) -> None:
    setup.register(MBNA)
    text = "\n".join(
        line
        for line in fixture("mbna_statement.txt").splitlines()
        if "Your Minimum Payment   $" not in line
    )
    doc = setup.file(MBNA_STATEMENT, text)

    assert setup.parse(doc).status == "parsed"
    period = setup.period(doc)
    assert period.minimum_due is None
    assert (period.balance_due, period.due_date) == (
        Decimal("1086.17"), dt.date(2025, 10, 14),
    )  # fmt: skip


def test_an_mbna_statement_for_an_unregistered_card_is_rejected(setup: Setup) -> None:
    setup.register(VISA)
    doc = setup.file(MBNA_STATEMENT, fixture("mbna_statement.txt"))

    assert setup.parse(doc) == Parsed("rejected", "unknown_account")


# --- registration --------------------------------------------------------------------


def test_the_card_parsers_register_as_hint_setting() -> None:
    """Their lines carry hints, so a patterns edit must parse their documents again."""
    registered = {
        parser.parser_id: (parser.version, parser.sets_hints)
        for issuer, fmt in (("vancity-visa", "pdf"), ("vancity-visa", "csv"),
                            ("mbna", "pdf"))
        for parser in parsers_for(issuer, fmt)
    }  # fmt: skip
    assert registered == {
        "vancity_visa.statement": ("1", True),
        "vancity_visa.transactions": ("transactions-1", True),
        "vancity_visa.csv": ("1", True),
        "mbna.statement": ("1", True),
    }
