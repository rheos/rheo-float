"""The Nelson & District Credit Union statement (two layout versions), the Wise
transaction history and the PayPal activity statement through ``parse_document``
(FR-2 ``official``, FR-3, FR-8, FR-15(a), FR-15(c); AC-8, AC-58 and AC-85 inputs).

Seam: ``parse_document`` over a synthetic document filed into a temporary archive,
against accounts registered through the manual files. Each control kind ties out
its own way: the NDCU statement is ``summary`` per account (a one-cent change
rejects it), PayPal is ``summary`` per currency, and the Wise history prints no
balance, so it is ``none`` and ``official=false``. An NDCU e-Transfer debit, its
cancellation and the debit again give a reversal hint on the credit only. Every
value is invented; the fixtures are layout text, read as text where the real run
calls pdftotext.
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
from sqlalchemy import Connection, select

from rheo_finance.context import UnitOfWork
from rheo_finance.documents.registry import document_id, file_archive
from rheo_finance.manual.accounts import ACCOUNTS_FILE, IDENTIFIERS_FILE
from rheo_finance.manual.headers import MANUAL_FILES
from rheo_finance.manual.stage2 import account_id, load_accounts
from rheo_finance.parsers.base import NotRecognised
from rheo_finance.parsers.registry import Parsed, parse_document, parsers_for
from rheo_finance.storage.schema import document, document_line, statement_period

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "synthetic"
NOW = dt.datetime(2026, 5, 2, 12, 0, tzinfo=dt.UTC)
D = dt.date


@dataclasses.dataclass(frozen=True)
class Account:
    slug: str
    institution: str
    kind: str
    currencies: str
    identifier: str


CHEQUING = Account(
    "cu-chequing", "Nelson & District Credit Union", "chequing", "CAD", "999000000101"
)
SHARES = Account(
    "cu-shares", "Nelson & District Credit Union", "savings", "CAD", "999000000102"
)
WISE = Account("wise", "Wise", "wallet", "CAD;USD", "P99900001")
PAYPAL = Account("paypal", "PayPal", "payment_processor", "CAD;USD", "SAMPLE999A9ID")


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

    def register(self, *accounts: Account) -> None:
        manual = self.data / "manual"
        manual.mkdir(parents=True, exist_ok=True)
        columns = MANUAL_FILES[ACCOUNTS_FILE]
        rows = [",".join(columns)]
        for each in accounts:
            cells = dict.fromkeys(columns, "")
            cells.update(
                slug=each.slug, label=each.slug.title(), institution=each.institution,
                kind=each.kind, currencies=each.currencies, cadence="monthly",
            )  # fmt: skip
            rows.append(",".join(cells[column] for column in columns))
        (manual / ACCOUNTS_FILE).write_text("\n".join(rows) + "\n", encoding="utf-8")
        identifiers = [",".join(MANUAL_FILES[IDENTIFIERS_FILE])]
        identifiers += [f"{each.slug},{each.identifier},number,," for each in accounts]
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

    def document(self, doc: uuid.UUID) -> Any:
        return self.conn.execute(select(document).where(document.c.id == doc)).one()

    def periods(self, doc: uuid.UUID) -> list[Any]:
        return list(
            self.conn.execute(
                select(statement_period)
                .where(statement_period.c.document_id == doc)
                .order_by(statement_period.c.account_id, statement_period.c.currency)
            ).all()
        )

    def lines(self, doc: uuid.UUID) -> list[Any]:
        return list(
            self.conn.execute(
                select(document_line)
                .where(document_line.c.document_id == doc)
                .order_by(document_line.c.line_no)
            ).all()
        )

    def stored_text(self, doc: uuid.UUID) -> str:
        found: list[str] = []
        for table in (document, statement_period, document_line):
            key = table.c.id if table is document else table.c.document_id
            for row in self.conn.execute(select(table).where(key == doc)):
                found += [str(value).casefold() for value in row if value is not None]
        return "\n".join(found)


@pytest.fixture
def setup(owner_conn: Connection, tmp_path: Path) -> Setup:
    return Setup(owner_conn, tmp_path)


def hints(line: Any) -> tuple[bool, bool, bool, bool, str | None]:
    return (
        line.transfer_hint,
        line.reversal_hint,
        line.conversion_hint,
        line.etransfer_hint,
        line.hint_account_slug,
    )


NO_HINT = (False, False, False, False, None)


# --- Nelson & District Credit Union ---------------------------------------------------

CURRENT = "nelson-district-cu/2025-11-30.pdf"
AUGUST_2018 = "nelson-district-cu/2018-08-31.pdf"
CURRENT_TEXT = "ndcu_statement_current.txt"


def test_the_current_layout_ties_out_per_account(setup: Setup) -> None:
    setup.register(CHEQUING, SHARES)
    doc = setup.file(CURRENT, fixture(CURRENT_TEXT))

    assert setup.parse(doc) == Parsed("parsed", None, periods=2, lines=7)
    row = setup.document(doc)
    assert (row.parser_id, row.parser_version, row.official) == (
        "nelson_district_cu.statement", "1", True,
    )  # fmt: skip
    by_account = {period.account_id: period for period in setup.periods(doc)}
    chequing = by_account[account_id("cu-chequing")]
    assert (chequing.control_kind, chequing.tied_out, chequing.currency) == (
        "summary", True, "CAD",
    )  # fmt: skip
    assert (chequing.period_start, chequing.period_end) == (
        D(2025, 11, 1), D(2025, 11, 30),
    )  # fmt: skip
    assert (chequing.opening, chequing.closing) == (
        Decimal("3100.00"), Decimal("2270.81"),
    )  # fmt: skip
    assert (chequing.total_debits, chequing.total_credits) == (
        Decimal("2476.12"), Decimal("1646.93"),
    )  # fmt: skip
    shares = by_account[account_id("cu-shares")]
    assert (shares.opening, shares.closing, shares.tied_out) == (
        Decimal("15.00"), Decimal("15.00"), True,
    )  # fmt: skip


def test_a_cancelled_debit_gives_a_reversal_hint_on_the_credit_only(
    setup: Setup,
) -> None:
    """An e-Transfer debit, the bank's reversal back in (no payee printed), and the
    same debit again: one reversal hint, on the credit."""
    setup.register(CHEQUING, SHARES)
    doc = setup.file(CURRENT, fixture(CURRENT_TEXT))
    setup.parse(doc)

    lines = setup.lines(doc)
    shape = [
        (line.posting_date, line.amount, line.reversal_hint, line.counterparty)
        for line in lines[:3]
    ]
    assert shape == [
        (D(2025, 11, 4), Decimal("-1234.56"), False, "sample payee"),
        (D(2025, 11, 9), Decimal("1234.56"), True, None),
        (D(2025, 11, 10), Decimal("-1234.56"), False, "sample payee"),
    ]
    assert [line.reversal_hint for line in lines].count(True) == 1
    assert all(line.etransfer_hint for line in lines[:4])
    # A payer's name wraps onto the next line; the counterparty holds all of it.
    assert (lines[3].amount, lines[3].counterparty) == (
        Decimal("412.30"), "sample payer",
    )  # fmt: skip
    assert lines[3].description_raw.endswith("Payer: SAMPLE PAYER")
    # The fees and the interest carry no transfer, reversal or e-Transfer hint.
    assert [hints(line) for line in lines[4:]] == [NO_HINT] * 3
    assert [line.fee_type for line in lines[4:]] == ["service_charge", "fee", None]


def test_the_2018_layout_is_its_own_version(setup: Setup) -> None:
    setup.register(CHEQUING, SHARES)
    doc = setup.file(AUGUST_2018, fixture("ndcu_statement_2018.txt"))

    assert setup.parse(doc) == Parsed("parsed", None, periods=2, lines=5)
    row = setup.document(doc)
    assert (row.parser_id, row.parser_version) == (
        "nelson_district_cu.statement_2018", "2",
    )  # fmt: skip
    lines = setup.lines(doc)
    assert [line.amount for line in lines] == [
        Decimal(each) for each in ("200.00", "-1.00", "-300.00", "-4.00", "0.01")
    ]
    assert lines[0].description_raw == "ATM Deposit SAMPLE BRANCH ANYTOWN"
    assert (lines[2].etransfer_hint, lines[2].counterparty) == (True, "sample payee")
    chequing = next(
        period for period in setup.periods(doc)
        if period.account_id == account_id("cu-chequing")
    )  # fmt: skip
    assert (chequing.period_start, chequing.period_end, chequing.tied_out) == (
        D(2018, 8, 1), D(2018, 8, 31), True,
    )  # fmt: skip


def test_each_version_reads_only_its_own_layout() -> None:
    by_version = {p.version: p for p in parsers_for("nelson-district-cu", "pdf")}
    current, older = by_version["1"].filing_date, by_version["2"].filing_date
    assert current is not None and older is not None
    assert current(fixture(CURRENT_TEXT)) == D(2025, 11, 30)
    assert older(fixture("ndcu_statement_2018.txt")) == D(2018, 8, 31)
    with pytest.raises(NotRecognised):
        current(fixture("ndcu_statement_2018.txt"))
    with pytest.raises(NotRecognised):
        older(fixture(CURRENT_TEXT))


def test_one_cent_off_the_totals_rejects_the_statement(setup: Setup) -> None:
    setup.register(CHEQUING, SHARES)
    text = fixture(CURRENT_TEXT)
    assert text.count("$1,646.93") == 1
    doc = setup.file(CURRENT, text.replace("$1,646.93", "$1,646.94"))

    assert setup.parse(doc) == Parsed("rejected", "summary_mismatch")
    assert setup.lines(doc) == []


def test_a_balance_the_lines_do_not_reach_rejects_the_statement(setup: Setup) -> None:
    setup.register(CHEQUING, SHARES)
    text = fixture(CURRENT_TEXT)
    broken = "1,234.56   3,100.00"
    assert text.count(broken) == 1
    doc = setup.file(CURRENT, text.replace(broken, "1,234.56   3,100.01"))

    assert setup.parse(doc) == Parsed("rejected", "checks_failed")


def test_an_unregistered_account_number_rejects_the_statement(setup: Setup) -> None:
    setup.register(CHEQUING)  # not the share account
    doc = setup.file(CURRENT, fixture(CURRENT_TEXT))

    assert setup.parse(doc) == Parsed("rejected", "unknown_account")


@pytest.mark.parametrize(
    "footer",
    ["   www.example.test", "   Page   1 of 2", "   Sample Branch 100 Sample St."],
)
def test_footer_text_after_a_transaction_never_joins_its_description(
    setup: Setup, footer: str
) -> None:
    """Footer or header text right after a transaction line, with no continued
    marker or blank line between, is a layout the parser has not seen: the
    statement is unparsed, and no description takes the text."""
    setup.register(CHEQUING, SHARES)
    text = fixture(CURRENT_TEXT)
    last = "28 Nov 25   Credit Interest   0.07   2,270.81\n"
    assert text.count(last) == 1
    doc = setup.file(CURRENT, text.replace(last, last + footer + "\n"))

    assert setup.parse(doc) == Parsed("unparsed", "not_recognised")
    assert setup.lines(doc) == []


@pytest.mark.parametrize("footer", ["   Page 1 of 2", "   www.example.test"])
def test_a_wrap_that_is_not_name_shaped_is_unparsed_even_before_the_marker(
    setup: Setup, footer: str
) -> None:
    """A page number or a web address between a wrapped name and the continued
    marker is in the right place for a wrap but not its shape: the statement is
    unparsed rather than the text joining the description."""
    setup.register(CHEQUING, SHARES)
    text = fixture(CURRENT_TEXT)
    wrap = "   PAYER\n   - con t inued -\n"
    assert text.count(wrap) == 1
    doc = setup.file(
        CURRENT, text.replace(wrap, f"   PAYER\n{footer}\n   - con t inued -\n")
    )

    assert setup.parse(doc) == Parsed("unparsed", "not_recognised")
    assert setup.lines(doc) == []


def test_a_wrapped_name_still_joins_before_the_next_line(setup: Setup) -> None:
    setup.register(CHEQUING, SHARES)
    doc = setup.file(AUGUST_2018, fixture("ndcu_statement_2018.txt"))
    setup.parse(doc)

    assert setup.lines(doc)[0].description_raw == "ATM Deposit SAMPLE BRANCH ANYTOWN"


@pytest.mark.parametrize("heading", ["USD CHEQUING", "LOANS", "TERM DEPOSITS"])
def test_an_unknown_section_heading_is_not_read_as_the_section_before(
    setup: Setup, heading: str
) -> None:
    """The share section's heading replaced by another: its account must not take
    the chequing kind, and a USD section is never read as CAD."""
    setup.register(CHEQUING, SHARES)
    text = fixture(CURRENT_TEXT)
    assert text.count("\nSHARES\n") == 1
    doc = setup.file(CURRENT, text.replace("\nSHARES\n", f"\n{heading}\n"))

    assert setup.parse(doc) == Parsed("unparsed", "not_recognised")


def test_an_unknown_heading_under_a_known_one_is_not_recognised(setup: Setup) -> None:
    """An unknown capitals heading between a known heading and its account line: the
    account keeps a heading of its own above it, so only the unknown heading itself
    can make the statement unparsed."""
    setup.register(CHEQUING, SHARES)
    text = fixture(CURRENT_TEXT)
    assert text.count("\nSHARES\n") == 1
    doc = setup.file(CURRENT, text.replace("\nSHARES\n", "\nSHARES\nTERM DEPOSITS\n"))

    assert setup.parse(doc) == Parsed("unparsed", "not_recognised")


def test_an_account_line_without_its_own_heading_is_not_recognised(
    setup: Setup,
) -> None:
    setup.register(CHEQUING, SHARES)
    text = fixture(CURRENT_TEXT)
    assert text.count("\nSHARES\n") == 1
    doc = setup.file(CURRENT, text.replace("\nSHARES\n", "\n"))

    assert setup.parse(doc) == Parsed("unparsed", "not_recognised")


def test_the_account_numbers_and_the_members_name_are_stored_nowhere(
    setup: Setup,
) -> None:
    setup.register(CHEQUING, SHARES)
    doc = setup.file(CURRENT, fixture(CURRENT_TEXT))
    setup.parse(doc)

    stored = setup.stored_text(doc)
    for value in ("999000000101", "999000000102", "99900001", "sample a member"):
        assert value not in stored


# --- Wise -----------------------------------------------------------------------------

WISE_HISTORY = "wise/transactions-2026-02-01_to_2026-02-28.pdf"


def test_the_wise_history_is_unofficial_and_unverified_per_currency(
    setup: Setup,
) -> None:
    setup.register(WISE)
    doc = setup.file(WISE_HISTORY, fixture("wise_history.txt"))

    assert setup.parse(doc) == Parsed("parsed", None, periods=2, lines=11)
    row = setup.document(doc)
    assert (row.parser_id, row.official) == ("wise.history", False)
    periods = setup.periods(doc)
    assert [(p.currency, p.control_kind, p.tied_out) for p in periods] == [
        ("CAD", "none", False), ("USD", "none", False),
    ]  # fmt: skip
    assert {(p.period_start, p.period_end) for p in periods} == {
        (D(2026, 2, 1), D(2026, 2, 28))
    }


def test_the_wise_lines_read_side_fee_and_currency_from_the_layout(
    setup: Setup,
) -> None:
    setup.register(WISE)
    doc = setup.file(WISE_HISTORY, fixture("wise_history.txt"))
    setup.parse(doc)

    got = [
        (line.posting_date, line.amount, line.currency, line.fee_type,
         line.description_raw)
        for line in setup.lines(doc)
    ]  # fmt: skip
    assert got == [
        # A fee larger than what arrived keeps its role: read by its cell.
        (D(2026, 2, 8), Decimal("-3.00"), "CAD", None, "Sent | Sample Payee Two"),
        (D(2026, 2, 8), Decimal("-5.00"), "CAD", "fee",
         "Sent | Sample Payee Two (fee)"),
        # Sent with a fee: what arrived, and the fee on its own line.
        (D(2026, 2, 10), Decimal("-98.80"), "CAD", None, "Sent | Sample Payee"),
        (D(2026, 2, 10), Decimal("-1.20"), "CAD", "fee", "Sent | Sample Payee (fee)"),
        # A card paid in USD from the CAD balance, its conversion fee apart.
        (D(2026, 2, 12), Decimal("-16.50"), "CAD", None, "Sample Web Shop"),
        (D(2026, 2, 12), Decimal("-0.30"), "CAD", "conversion_fee",
         "Sample Web Shop (fee)"),
        (D(2026, 2, 15), Decimal("-45.10"), "CAD", None, "Sample Grocer"),
        # A conversion: out-leg, in-leg, and the printed fee as the only fee line.
        (D(2026, 2, 20), Decimal("-100.00"), "USD", None, "Moved | To CAD"),
        (D(2026, 2, 20), Decimal("135.20"), "CAD", None, "Moved | To CAD"),
        (D(2026, 2, 20), Decimal("-0.80"), "USD", "conversion_fee",
         "Moved | To CAD (fee)"),
        (D(2026, 2, 27), Decimal("250.00"), "USD", None, "STRIPE PAYMENTS CANADA"),
    ]  # fmt: skip
    card = setup.lines(doc)[4]
    assert (card.original_amount, card.original_currency) == (Decimal("-12.00"), "USD")


def test_a_wise_fee_and_amount_on_separate_lines_must_hold_a_zero(
    setup: Setup,
) -> None:
    """With no line holding two amounts, only a zero fee can be told apart."""
    setup.register(WISE)
    text = fixture("wise_history.txt")
    shared = "Sent | Sample Payee Two   5.00 CAD   3.00 CAD\n"
    assert text.count(shared) == 1
    split = "Sent | Sample Payee Two   5.00 CAD\n   3.00 CAD\n"
    doc = setup.file(WISE_HISTORY, text.replace(shared, split))

    assert setup.parse(doc) == Parsed("unparsed", "not_recognised")


def test_the_wise_hints_mark_the_conversion_legs_and_the_stripe_payout(
    setup: Setup,
) -> None:
    setup.register(WISE)
    doc = setup.file(WISE_HISTORY, fixture("wise_history.txt"))
    setup.parse(doc)

    lines = setup.lines(doc)
    assert [hints(line) for line in lines] == [
        NO_HINT, NO_HINT, NO_HINT, NO_HINT, NO_HINT, NO_HINT, NO_HINT,
        (False, False, True, False, None),
        (False, False, True, False, None),
        NO_HINT,  # the fee line is never taken for a conversion leg
        (True, False, False, False, "stripe"),
    ]  # fmt: skip
    counterparties = [line.counterparty for line in lines]
    assert counterparties == [
        "Sample Payee Two", None, "Sample Payee", None, None, None, None, None,
        None, None, "STRIPE PAYMENTS CANADA",
    ]  # fmt: skip


def test_the_wise_membership_number_is_stored_nowhere(setup: Setup) -> None:
    setup.register(WISE)
    doc = setup.file(WISE_HISTORY, fixture("wise_history.txt"))
    setup.parse(doc)

    stored = setup.stored_text(doc)
    assert "99900001" not in stored and "sample holder" not in stored


def test_a_wise_letter_that_is_not_a_history_is_unparsed(setup: Setup) -> None:
    setup.register(WISE)
    letter = "Wise Payments Canada Inc.\n\nProof of account details\n\nAccount number\n"
    doc = setup.file("wise/account-details.pdf", letter)

    assert setup.parse(doc) == Parsed("unparsed", "not_recognised")


# --- PayPal ---------------------------------------------------------------------------

PAYPAL_STATEMENT = "paypal/activity-2026-04-01_to_2026-04-30.pdf"


def test_paypal_gives_one_tied_out_summary_period_per_currency(setup: Setup) -> None:
    setup.register(PAYPAL)
    doc = setup.file(PAYPAL_STATEMENT, fixture("paypal_activity.txt"))

    assert setup.parse(doc) == Parsed("parsed", None, periods=2, lines=6)
    periods = setup.periods(doc)
    assert [
        (p.currency, p.control_kind, p.tied_out, p.opening, p.closing) for p in periods
    ] == [
        ("CAD", "summary", True, Decimal("10.00"), Decimal("0.00")),
        ("USD", "summary", True, Decimal("5.00"), Decimal("19.20")),
    ]
    assert {(p.period_start, p.period_end) for p in periods} == {
        (D(2026, 4, 1), D(2026, 4, 30))
    }


def test_paypal_lines_carry_the_bank_pull_and_conversion_hints(setup: Setup) -> None:
    setup.register(PAYPAL)
    doc = setup.file(PAYPAL_STATEMENT, fixture("paypal_activity.txt"))
    setup.parse(doc)

    got = [
        (line.posting_date, line.currency, line.amount, hints(line),
         line.counterparty, line.fee_type)
        for line in setup.lines(doc)
    ]  # fmt: skip
    conversion = (False, False, True, False, None)
    assert got == [
        (D(2026, 4, 3), "CAD", Decimal("30.00"),
         (True, False, False, False, None), None, None),
        (D(2026, 4, 3), "CAD", Decimal("-40.00"), conversion, None, None),
        (D(2026, 4, 3), "USD", Decimal("28.50"), conversion, None, None),
        (D(2026, 4, 4), "USD", Decimal("-33.50"), NO_HINT, "Sample Streaming Co",
         None),
        (D(2026, 4, 20), "USD", Decimal("20.00"), NO_HINT, "Sample Client Inc.",
         None),
        (D(2026, 4, 20), "USD", Decimal("-0.80"), NO_HINT, None, "fee"),
    ]  # fmt: skip
    pull = setup.lines(doc)[3]
    assert pull.description_raw == (
        "PreApproved Payment Bill User Payment Sample Streaming Co"
    )


def test_paypal_one_cent_off_the_closing_rejects_the_statement(setup: Setup) -> None:
    setup.register(PAYPAL)
    text = fixture("paypal_activity.txt")
    closing = " USD   5.00   19.20"
    assert text.count(closing) == 1
    doc = setup.file(PAYPAL_STATEMENT, text.replace(closing, " USD   5.00   19.21"))

    assert setup.parse(doc) == Parsed("rejected", "summary_mismatch")


def test_an_account_id_is_matched_on_its_letters_too(setup: Setup) -> None:
    """An ID with the same digits and other letters is another account."""
    setup.register(dataclasses.replace(PAYPAL, identifier="OTHERS999B9XX"))
    doc = setup.file(PAYPAL_STATEMENT, fixture("paypal_activity.txt"))
    assert setup.parse(doc) == Parsed("rejected", "unknown_account")

    setup.register(dataclasses.replace(PAYPAL, identifier="sample-999a-9id"))
    assert setup.parse(doc).status == "parsed"


def test_paypal_ids_and_emails_are_stored_nowhere(setup: Setup) -> None:
    setup.register(PAYPAL)
    doc = setup.file(PAYPAL_STATEMENT, fixture("paypal_activity.txt"))
    setup.parse(doc)

    stored = setup.stored_text(doc)
    for value in ("sample999a9id", "@", "9sample9990000"):
        assert value not in stored


# --- registration ---------------------------------------------------------------------


def test_the_new_parsers_register_as_hint_setting() -> None:
    registered = {
        parser.parser_id: (parser.version, parser.sets_hints)
        for issuer in ("nelson-district-cu", "wise", "paypal")
        for parser in parsers_for(issuer, "pdf")
    }
    assert registered == {
        "nelson_district_cu.statement": ("1", True),
        "nelson_district_cu.statement_2018": ("2", True),
        "wise.history": ("1", True),
        "paypal.activity": ("1", True),
    }
