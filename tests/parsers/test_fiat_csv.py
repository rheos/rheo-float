"""The PayPal activity CSV, the Wise statement CSV and the Stripe balance history CSV
through ``parse_document`` (FR-12(a), FR-2, FR-3, FR-15(c); AC-80 parse, control and
unparsed clauses; edge case 22).

Seam: ``parse_document`` over a synthetic export filed into a temporary archive,
against accounts registered through the manual files. The PayPal and Wise exports
print a balance, so each is ``running_balance`` per currency: continuous balances tie
a period out and a break is a gap. The Stripe export prints none, so it is ``none``,
and each payout gives its gross, its fees and the payout debit, which carries the
Wise hint. A header that does not match is ``unparsed`` with no line. The conversion
legs carry ``conversion_hint``, and a fee is its own line. Every value is invented;
the fixtures are shaped on the vendors' documented exports.
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
from rheo_finance.manual.stage2 import account_id
from rheo_finance.parsers.registry import Parsed, parse_document, parsers_for
from rheo_finance.storage.schema import document, document_line, statement_period

from .conftest import ManualAccount, register_accounts

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "synthetic"
NOW = dt.datetime(2026, 5, 2, 12, 0, tzinfo=dt.UTC)
D = dt.date
PAYPAL = ManualAccount(
    "paypal", "PayPal", "payment_processor", ("SAMPLE999A9ID",), currencies="CAD;USD"
)
WISE = ManualAccount("wise", "Wise", "wallet", ("P99900001",), currencies="CAD;USD")
STRIPE = ManualAccount(
    "stripe", "Stripe", "payment_processor", ("SAMPLE999STRIPE",), currencies="USD"
)
PAYPAL_CSV = "paypal/activity-2026-04-30.csv"
WISE_CSV = "wise/statement-2026-02-28.csv"
STRIPE_CSV = "stripe/balance-2026-02-26.csv"


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


class Setup:
    def __init__(self, conn: Connection, tmp_path: Path) -> None:
        self.conn = conn
        self.uow = UnitOfWork(conn)
        self.data = tmp_path / "data"
        self.archive = tmp_path / "archive"
        self.archive.mkdir()

    def register(self, *accounts: ManualAccount) -> None:
        register_accounts(self.uow, self.data, *accounts)

    def file(self, relative: str, text: str) -> uuid.UUID:
        target = self.archive / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        data = text.encode("utf-8")
        target.write_bytes(data)
        file_archive(self.uow, self.archive, self.data, NOW)
        return document_id(hashlib.sha256(data).hexdigest())

    def parse(self, doc: uuid.UUID) -> Parsed:
        return parse_document(self.uow, doc, self.archive)

    def document(self, doc: uuid.UUID) -> Any:
        return self.conn.execute(select(document).where(document.c.id == doc)).one()

    def periods(self, doc: uuid.UUID) -> list[Any]:
        return list(
            self.conn.execute(
                select(statement_period)
                .where(statement_period.c.document_id == doc)
                .order_by(statement_period.c.currency)
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
CONVERSION = (False, False, True, False, None)


def period_view(period: Any) -> tuple[object, ...]:
    return (
        period.account_id, period.currency, period.period_start, period.period_end,
        period.control_kind, period.tied_out,
    )  # fmt: skip


# --- PayPal activity CSV --------------------------------------------------------------


def test_paypal_csv_is_running_balance_per_currency(setup: Setup) -> None:
    setup.register(PAYPAL)
    doc = setup.file(PAYPAL_CSV, fixture("paypal_activity.csv"))

    assert setup.parse(doc) == Parsed("parsed", None, periods=2, lines=7, segments=2)

    paypal = account_id("paypal")
    assert [period_view(each) for each in setup.periods(doc)] == [
        (paypal, "CAD", D(2026, 3, 28), D(2026, 4, 3), "running_balance", True),
        (paypal, "USD", D(2026, 4, 3), D(2026, 4, 20), "running_balance", True),
    ]
    row = setup.document(doc)
    assert (row.kind, row.parser_id, row.official) == (
        "export", "paypal.activity_csv", True,
    )  # fmt: skip


def test_paypal_csv_lines_match_the_statement_shape(setup: Setup) -> None:
    setup.register(PAYPAL)
    doc = setup.file(PAYPAL_CSV, fixture("paypal_activity.csv"))
    setup.parse(doc)

    got = [
        (line.posting_date, line.amount, line.currency, line.fee_type,
         line.description_raw, line.running_balance, hints(line))
        for line in setup.lines(doc)
    ]  # fmt: skip
    assert got == [
        (D(2026, 3, 28), Decimal("-12.00"), "CAD", None,
         "Express Checkout Payment Sample Shop", Decimal("10.00"), NO_HINT),
        (D(2026, 4, 3), Decimal("30.00"), "CAD", None,
         "Bank Deposit to PP Account", Decimal("40.00"),
         (True, False, False, False, None)),
        (D(2026, 4, 3), Decimal("-40.00"), "CAD", None,
         "General Currency Conversion", Decimal("0.00"), CONVERSION),
        (D(2026, 4, 3), Decimal("28.50"), "USD", None,
         "General Currency Conversion", Decimal("33.50"), CONVERSION),
        # The Memo authorization before it gives no line.
        (D(2026, 4, 4), Decimal("-33.50"), "USD", None,
         "PreApproved Payment Bill User Payment Sample Streaming Co",
         Decimal("0.00"), NO_HINT),
        # A fee is its own line, gross first, as the statement gives them; only the
        # row's last line carries the balance the row prints.
        (D(2026, 4, 20), Decimal("20.00"), "USD", None,
         "Mass Pay Payment Sample Client Inc.", None, NO_HINT),
        (D(2026, 4, 20), Decimal("-0.80"), "USD", "fee",
         "Mass Pay Payment Sample Client Inc. (fee)", Decimal("19.20"), NO_HINT),
    ]  # fmt: skip
    assert [line.counterparty for line in setup.lines(doc)] == [
        "Sample Shop", None, None, None, "Sample Streaming Co", "Sample Client Inc.",
        None,
    ]  # fmt: skip


def test_a_paypal_balance_that_does_not_continue_is_a_gap(setup: Setup) -> None:
    setup.register(PAYPAL)
    text = fixture("paypal_activity.csv")
    printed = '"1","","10.00"'  # the March purchase's balance
    assert text.count(printed) == 1
    doc = setup.file(PAYPAL_CSV, text.replace(printed, '"1","","11.00"'))

    parsed = setup.parse(doc)

    assert (parsed.status, parsed.lines, parsed.gaps) == ("parsed", 7, 1)
    cad = setup.periods(doc)[0]
    assert (cad.control_kind, cad.tied_out) == ("running_balance", False)


def test_a_currency_of_one_row_with_a_fee_does_not_tie_out(setup: Setup) -> None:
    """A row's own lines never verify each other: one row is no segment."""
    setup.register(PAYPAL)
    header, *rows = fixture("paypal_activity.csv").splitlines()
    (fee_row,) = [row for row in rows if '"-0.80"' in row]
    doc = setup.file(PAYPAL_CSV, f"{header}\n{fee_row}\n")

    assert setup.parse(doc) == Parsed("parsed", None, periods=1, lines=2)
    (usd,) = setup.periods(doc)
    assert (usd.currency, usd.control_kind, usd.tied_out) == (
        "USD", "running_balance", False,
    )  # fmt: skip


def test_a_paypal_csv_with_another_header_is_unparsed(setup: Setup) -> None:
    setup.register(PAYPAL)
    text = fixture("paypal_activity.csv").replace('"Balance Impact"', '"Impact"', 1)
    doc = setup.file(PAYPAL_CSV, text)

    assert setup.parse(doc) == Parsed("unparsed", "not_recognised")
    assert setup.lines(doc) == []
    assert setup.periods(doc) == []


def test_a_paypal_row_whose_net_is_not_gross_plus_fee_is_unparsed(setup: Setup) -> None:
    setup.register(PAYPAL)
    text = fixture("paypal_activity.csv")
    printed = '"20.00","-0.80","19.20"'
    assert text.count(printed) == 1
    doc = setup.file(PAYPAL_CSV, text.replace(printed, '"20.00","-0.80","19.30"'))

    assert setup.parse(doc) == Parsed("unparsed", "not_recognised")


def test_the_paypal_csv_takes_the_one_paypal_account(setup: Setup) -> None:
    """The export prints no account identifier: none or two PayPal accounts reject."""
    setup.register(WISE)
    doc = setup.file(PAYPAL_CSV, fixture("paypal_activity.csv"))
    assert setup.parse(doc) == Parsed("rejected", "unknown_account")

    second = dataclasses.replace(PAYPAL, slug="paypal-two", identifiers=("OTHER555X1",))
    setup.register(PAYPAL, second)
    assert setup.parse(doc) == Parsed("rejected", "ambiguous_account")


def test_only_a_numbered_account_of_the_vendor_kind_is_a_candidate(
    setup: Setup,
) -> None:
    """Another kind of account at the vendor, or one known by a card alone, does not
    make the file ambiguous."""
    crypto = ManualAccount("pp-crypto", "PayPal", "crypto_wallet", ("PPC555X3",))
    card_only = ManualAccount(
        "pp-card", "PayPal", "payment_processor", ("9999 99** **** 5552",),
        "masked_card",
    )  # fmt: skip
    setup.register(PAYPAL, crypto, card_only)
    doc = setup.file(PAYPAL_CSV, fixture("paypal_activity.csv"))

    assert setup.parse(doc).status == "parsed"
    assert {each.account_id for each in setup.periods(doc)} == {account_id("paypal")}


def test_an_account_whose_number_ended_before_the_file_is_no_candidate(
    setup: Setup,
) -> None:
    replaced = ManualAccount(
        "pp-old", "PayPal", "payment_processor", ("OLDPP555X4",),
        currencies="CAD;USD", valid_to="2026-01-31",
    )  # fmt: skip
    setup.register(replaced)
    doc = setup.file(PAYPAL_CSV, fixture("paypal_activity.csv"))
    assert setup.parse(doc) == Parsed("rejected", "unknown_account")

    setup.register(replaced, PAYPAL)
    assert setup.parse(doc).status == "parsed"
    assert {each.account_id for each in setup.periods(doc)} == {account_id("paypal")}


def test_paypal_csv_emails_and_ids_are_stored_nowhere(setup: Setup) -> None:
    setup.register(PAYPAL)
    doc = setup.file(PAYPAL_CSV, fixture("paypal_activity.csv"))
    setup.parse(doc)

    stored = setup.stored_text(doc)
    assert "@" not in stored
    assert "9sample999000" not in stored
    assert "sample item" not in stored and "pdt" not in stored


# --- Wise statement CSV ---------------------------------------------------------------


def test_wise_csv_is_running_balance_per_currency_and_official(setup: Setup) -> None:
    setup.register(WISE)
    doc = setup.file(WISE_CSV, fixture("wise_statement.csv"))

    assert setup.parse(doc) == Parsed("parsed", None, periods=2, lines=11, segments=2)

    wise = account_id("wise")
    assert [period_view(each) for each in setup.periods(doc)] == [
        (wise, "CAD", D(2026, 2, 8), D(2026, 2, 20), "running_balance", True),
        (wise, "USD", D(2026, 2, 20), D(2026, 2, 27), "running_balance", True),
    ]
    row = setup.document(doc)
    assert (row.kind, row.parser_id, row.official) == (
        "export", "wise.statement_csv", True,
    )  # fmt: skip


def test_wise_csv_lines_match_the_history_shape(setup: Setup) -> None:
    """The amounts, signs and fee lines of ``wise_history.txt``'s transactions."""
    setup.register(WISE)
    doc = setup.file(WISE_CSV, fixture("wise_statement.csv"))
    setup.parse(doc)

    lines = setup.lines(doc)
    got = [
        (line.posting_date, line.amount, line.currency, line.fee_type,
         line.running_balance, hints(line))
        for line in lines
    ]  # fmt: skip
    # Only a row's last line carries the balance the row prints.
    assert got == [
        (D(2026, 2, 8), Decimal("-3.00"), "CAD", None, None, NO_HINT),
        (D(2026, 2, 8), Decimal("-5.00"), "CAD", "fee", Decimal("492.00"), NO_HINT),
        (D(2026, 2, 10), Decimal("-98.80"), "CAD", None, None, NO_HINT),
        (D(2026, 2, 10), Decimal("-1.20"), "CAD", "fee", Decimal("392.00"), NO_HINT),
        (D(2026, 2, 12), Decimal("-16.50"), "CAD", None, None, NO_HINT),
        (D(2026, 2, 12), Decimal("-0.30"), "CAD", "conversion_fee",
         Decimal("375.20"), NO_HINT),
        (D(2026, 2, 15), Decimal("-45.10"), "CAD", None, Decimal("330.10"), NO_HINT),
        # The conversion's in-leg; its out-leg carries the only fee line.
        (D(2026, 2, 20), Decimal("135.20"), "CAD", None, Decimal("465.30"),
         CONVERSION),
        (D(2026, 2, 20), Decimal("-100.00"), "USD", None, None, CONVERSION),
        (D(2026, 2, 20), Decimal("-0.80"), "USD", "conversion_fee",
         Decimal("99.20"), NO_HINT),
        (D(2026, 2, 27), Decimal("250.00"), "USD", None, Decimal("349.20"),
         (True, False, False, False, "stripe")),
    ]  # fmt: skip
    card = lines[4]
    assert (card.original_amount, card.original_currency) == (Decimal("-12.00"), "USD")
    assert [line.counterparty for line in lines] == [
        "Sample Payee Two", None, "Sample Payee", None, None, None, None, None, None,
        None, "STRIPE PAYMENTS CANADA",
    ]  # fmt: skip


def test_a_wise_csv_with_another_header_is_unparsed(setup: Setup) -> None:
    setup.register(WISE)
    text = fixture("wise_statement.csv").replace('"Running Balance"', '"Balance"', 1)
    doc = setup.file(WISE_CSV, text)

    assert setup.parse(doc) == Parsed("unparsed", "not_recognised")
    assert setup.lines(doc) == []


def test_a_wise_balance_that_does_not_continue_is_a_gap(setup: Setup) -> None:
    setup.register(WISE)
    text = fixture("wise_statement.csv")
    printed = '"-16.80","CAD"'
    assert text.count(printed) == 1
    doc = setup.file(WISE_CSV, text.replace(printed, '"-16.90","CAD"'))

    parsed = setup.parse(doc)

    assert (parsed.status, parsed.lines, parsed.gaps) == ("parsed", 11, 1)
    assert [each.tied_out for each in setup.periods(doc)] == [False, True]


def test_a_negative_wise_fee_is_unparsed(setup: Setup) -> None:
    setup.register(WISE)
    text = fixture("wise_statement.csv")
    printed = '"1.20",""'
    assert text.count(printed) == 1
    doc = setup.file(WISE_CSV, text.replace(printed, '"-1.20",""'))

    assert setup.parse(doc) == Parsed("unparsed", "not_recognised")


def test_wise_csv_ids_card_digits_and_names_are_stored_nowhere(setup: Setup) -> None:
    setup.register(WISE)
    doc = setup.file(WISE_CSV, fixture("wise_statement.csv"))
    setup.parse(doc)

    stored = setup.stored_text(doc)
    # The card's last four is too short to look for among stored ids; its column is
    # read by no line, as the holder's name beside it shows.
    for printed in (
        "999000001", "999000777", "999000888", "sample holder", "sample ref",
        "sample note",
    ):  # fmt: skip
        assert printed not in stored, printed


# --- Stripe balance history CSV -------------------------------------------------------


def stripe_lines(setup: Setup, doc: uuid.UUID) -> list[tuple[object, ...]]:
    return [
        (line.posting_date, line.amount, line.currency, line.fee_type,
         line.description_raw, hints(line))
        for line in setup.lines(doc)
    ]  # fmt: skip


PAYOUT_HINT = (True, False, False, False, "wise")


def test_stripe_csv_gives_gross_fee_and_payout_per_payout(setup: Setup) -> None:
    setup.register(STRIPE)
    doc = setup.file(STRIPE_CSV, fixture("stripe_balance.csv"))

    assert setup.parse(doc) == Parsed("parsed", None, periods=1, lines=6)

    # No balance column: one unverified period over every row, the charge not yet
    # paid out included.
    assert [period_view(each) for each in setup.periods(doc)] == [
        (account_id("stripe"), "USD", D(2026, 2, 5), D(2026, 2, 26), "none", False),
    ]
    row = setup.document(doc)
    assert (row.kind, row.parser_id, row.official) == (
        "export", "stripe.balance_csv", True,
    )  # fmt: skip
    # Each payout, oldest first, on its created date: the gross of the rows it pays
    # out (a refund nets against the charges), one fee line, then the payout debit
    # carrying the Wise hint. The charge with no payout gives no line.
    assert stripe_lines(setup, doc) == [
        (D(2026, 2, 11), Decimal("40.00"), "USD", None, "Gross charges", NO_HINT),
        (D(2026, 2, 11), Decimal("-1.75"), "USD", "fee", "Stripe fees", NO_HINT),
        (D(2026, 2, 11), Decimal("-38.25"), "USD", None, "Payout to bank",
         PAYOUT_HINT),
        (D(2026, 2, 25), Decimal("258.40"), "USD", None, "Gross charges", NO_HINT),
        (D(2026, 2, 25), Decimal("-8.40"), "USD", "fee", "Stripe fees", NO_HINT),
        (D(2026, 2, 25), Decimal("-250.00"), "USD", None, "Payout to bank",
         PAYOUT_HINT),
    ]  # fmt: skip


def test_each_stripe_payout_reconciles_gross_less_fees(setup: Setup) -> None:
    setup.register(STRIPE)
    doc = setup.file(STRIPE_CSV, fixture("stripe_balance.csv"))
    setup.parse(doc)

    lines = setup.lines(doc)
    for gross, fee, payout in (lines[0:3], lines[3:6]):
        assert (gross.amount > 0, fee.fee_type, payout.transfer_hint) == (
            True, "fee", True,
        )  # fmt: skip
        assert gross.amount + fee.amount == -payout.amount
    assert {line.running_balance for line in lines} == {None}


def test_a_payout_partly_in_the_file_gives_its_payout_line_alone(
    setup: Setup,
) -> None:
    """An export cut by date: the later payout's rows do not add up to it, so only the
    payout debit (still hinted) is a line; a fuller export adds the gross and fee."""
    setup.register(STRIPE)
    text = fixture("stripe_balance.csv")
    (dropped,) = [row for row in text.splitlines() if "ch_999SAMPLE04" in row]
    doc = setup.file(STRIPE_CSV, text.replace(dropped + "\n", ""))

    assert setup.parse(doc) == Parsed("parsed", None, periods=1, lines=4)
    assert stripe_lines(setup, doc)[3:] == [
        (D(2026, 2, 25), Decimal("-250.00"), "USD", None, "Payout to bank",
         PAYOUT_HINT),
    ]  # fmt: skip


def test_a_stripe_csv_with_another_header_is_unparsed(setup: Setup) -> None:
    setup.register(STRIPE)
    text = fixture("stripe_balance.csv").replace('"Transfer"', '"Payout"', 1)
    doc = setup.file(STRIPE_CSV, text)

    assert setup.parse(doc) == Parsed("unparsed", "not_recognised")
    assert setup.lines(doc) == []
    assert setup.periods(doc) == []


@pytest.mark.parametrize(
    ("printed", "wrong"),
    [
        ('"120.00","3.78","116.22"', '"120.00","3.78","116.32"'),
        ('"120.00","3.78","116.22"', '"120.00","-3.78","123.78"'),
        ('"-250.00","0.00","-250.00"', '"250.00","0.00","250.00"'),
    ],
    ids=["net_not_amount_less_fee", "negative_fee", "payout_not_a_debit"],
)
def test_a_stripe_row_outside_the_layout_is_unparsed(
    setup: Setup, printed: str, wrong: str
) -> None:
    setup.register(STRIPE)
    text = fixture("stripe_balance.csv")
    assert text.count(printed) == 1
    doc = setup.file(STRIPE_CSV, text.replace(printed, wrong))

    assert setup.parse(doc) == Parsed("unparsed", "not_recognised")


def test_the_stripe_csv_takes_the_one_stripe_account(setup: Setup) -> None:
    """The export prints no account identifier: none or two Stripe accounts reject."""
    setup.register(PAYPAL)
    doc = setup.file(STRIPE_CSV, fixture("stripe_balance.csv"))
    assert setup.parse(doc) == Parsed("rejected", "unknown_account")

    second = dataclasses.replace(STRIPE, slug="stripe-two", identifiers=("OTHER555X2",))
    setup.register(STRIPE, second)
    assert setup.parse(doc) == Parsed("rejected", "ambiguous_account")


def test_stripe_csv_ids_descriptions_and_customers_are_stored_nowhere(
    setup: Setup,
) -> None:
    setup.register(STRIPE)
    doc = setup.file(STRIPE_CSV, fixture("stripe_balance.csv"))
    setup.parse(doc)

    stored = setup.stored_text(doc)
    for printed in ("999sample", "sample customer", "example.test", "stripe payout"):
        assert printed not in stored, printed
    assert {line.counterparty for line in setup.lines(doc)} == {None}


# --- registration ---------------------------------------------------------------------


def test_the_csv_parsers_register_as_hint_setting_exports() -> None:
    registered = {
        parser.parser_id: (parser.version, parser.sets_hints)
        for issuer in ("paypal", "wise", "stripe")
        for parser in parsers_for(issuer, "csv")
    }
    assert registered == {
        "paypal.activity_csv": ("1", True),
        "wise.statement_csv": ("1", True),
        "stripe.balance_csv": ("1", True),
    }
