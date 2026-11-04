"""Stage 14, paths (1) and (2): card statements and parsed bills (FR-19, FR-21; Key
decision 20; edge case 66; AC-87).

Seam: ``derive.run`` through the derive ``ledger`` over the synthetic statement and bill
layouts, read through the real parsers. Newer documents are the same fixtures with
invented dates and amounts written in. Every value is synthetic.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest
from sqlalchemy import Row, select

from rheo_finance.derive.stage14 import obligation_id
from rheo_finance.manual.accounts import ACCOUNTS_FILE, IDENTIFIERS_FILE
from rheo_finance.manual.payees import PAYEES_FILE
from rheo_finance.reports.views import DUPLICATE_BILL_MONTH
from rheo_finance.storage.schema import obligation, payee, report_status

from .support import FIXTURES, Ledger

D = dt.date
CARD = "9999 99** **** 9991"
MBNA_CARD = "9999 99XX XXXX 9992"
VISA_STATEMENT = "vancity-visa/2026-01-05.pdf"
NEWER_VISA_STATEMENT = "vancity-visa/2026-03-05.pdf"
VIRGIN = "virgin-plus/bill.pdf"
FORTIS = "fortisbc/bill.pdf"


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def accounts(ledger: Ledger, *rows: tuple[str, str, str, str, str]) -> None:
    """Accounts as ``(slug, institution, kind, identifier, payee patterns)``."""
    ledger.manual(
        ACCOUNTS_FILE,
        *(
            f"{slug},{slug.title()},{institution},{kind},CAD,monthly,,,,,,{patterns},,,"
            for slug, institution, kind, _, patterns in rows
        ),
    )
    ledger.manual(
        IDENTIFIERS_FILE,
        *(
            f"{slug},{identifier},"
            f"{'masked_card' if kind == 'credit_card' else 'number'},,"
            for slug, _, kind, identifier, _ in rows
        ),
    )


def visa(ledger: Ledger, patterns: str = "") -> None:
    accounts(ledger, ("visa", "Vancity", "credit_card", CARD, patterns))


def virgin_account(ledger: Ledger, patterns: str = "") -> None:
    accounts(ledger, ("mobile", "Virgin Plus", "bill_vendor", "999000123", patterns))


def fortis_account(ledger: Ledger) -> None:
    accounts(ledger, ("hydro", "FortisBC", "bill_vendor", "9990001234", ""))


def virgin_bill(
    bill: str = "February 2, 2026",
    due_short: str = "Feb 23, 2026",
    due_long: str = "February 23, 2026",
    total: str | None = None,
) -> str:
    """The Virgin bill fixture with an invented bill date, due date and total. A
    written total drops the printed unpaid balance, so the bill states no identity."""
    text = (
        fixture("virgin_bill.txt")
        .replace("February 2, 2026", bill)
        .replace("Feb 23, 2026", due_short)
        .replace("February 23, 2026", due_long)
    )
    if total is None:
        return text
    return text.replace("   Unpaid balance   $45.10\n", "").replace("126.30", total)


def fortis_bill(bill: str, due: str) -> str:
    return (
        fixture("fortisbc_bill.txt")
        .replace("January 15, 2026", bill)
        .replace("February 5, 2026", due)
    )


def rows(ledger: Ledger) -> list[Row[tuple[object, ...]]]:
    """The obligations, oldest issue first."""
    return list(
        ledger.conn.execute(
            select(obligation).order_by(
                obligation.c.issued_on, obligation.c.period_label
            )
        ).all()
    )


def payee_id(ledger: Ledger, slug: str) -> object:
    return ledger.conn.scalar(select(payee.c.id).where(payee.c.slug == slug))


def owed_total(ledger: Ledger) -> Decimal:
    return sum(
        (Decimal(row.remaining) for row in rows(ledger) if row.status != "superseded"),
        Decimal(0),
    )


def duplicate_months(ledger: Ledger) -> dict[str | None, int]:
    found = ledger.conn.execute(
        select(report_status.c.subject, report_status.c.count).where(
            report_status.c.fact == DUPLICATE_BILL_MONTH
        )
    ).all()
    return {subject: count for subject, count in found}


# --- path (1): card statements --------------------------------------------------------


def test_a_card_statement_gives_the_obligation_as_printed(ledger: Ledger) -> None:
    visa(ledger)
    ledger.file(VISA_STATEMENT, fixture("vancity_visa_statement.txt"))
    ledger.derive()

    (row,) = rows(ledger)

    assert (row.kind, row.period_label) == ("card_statement", "card-2026-01-05")
    assert row.statement_date == row.issued_on == D(2026, 1, 5)
    assert row.due_date == D(2026, 1, 30)
    assert row.amount_due == Decimal("1207.27")
    assert row.minimum_due == row.threshold_due == Decimal("36.22")
    assert row.paid == 0
    assert row.remaining == Decimal("1207.27")
    assert row.threshold_remaining == Decimal("36.22")
    assert row.status == "open"
    assert row.primary_source == VISA_STATEMENT
    assert row.supersedes_id is None
    assert row.manual_mismatch is None and row.payments_mismatch is None


def test_an_mbna_statement_gives_the_same_shape(ledger: Ledger) -> None:
    accounts(ledger, ("mbna-card", "MBNA", "credit_card", MBNA_CARD, ""))
    ledger.file("mbna/2025-09-20.pdf", fixture("mbna_statement.txt"))
    ledger.derive()

    (row,) = rows(ledger)

    assert (row.kind, row.period_label) == ("card_statement", "card-2025-09-20")
    assert (row.amount_due, row.minimum_due, row.threshold_due) == (
        Decimal("1086.17"),
        Decimal("32.00"),
        Decimal("32.00"),
    )
    assert row.due_date == D(2025, 10, 14)
    assert row.payee_id == payee_id(ledger, "mbna-card")


def test_a_statement_with_no_minimum_has_the_full_balance_as_its_threshold(
    ledger: Ledger,
) -> None:
    visa(ledger)
    ledger.file(NEWER_VISA_STATEMENT, fixture("vancity_visa_statement_no_minimum.txt"))
    ledger.derive()

    (row,) = rows(ledger)

    assert row.minimum_due is None
    assert row.threshold_due == row.threshold_remaining == Decimal("25.00")


@pytest.mark.parametrize(
    ("printed", "paid_in"), [("$0.00", "$425.00"), ("-$25.00", "$450.00")]
)
def test_a_statement_at_zero_or_in_credit_owes_nothing(
    ledger: Ledger, printed: str, paid_in: str
) -> None:
    visa(ledger)
    ledger.file(
        NEWER_VISA_STATEMENT,
        fixture("vancity_visa_statement_no_minimum.txt")
        .replace(
            "Payments & credits   -   $400.00", f"Payments & credits   -   {paid_in}"
        )
        .replace("- $400.00", f"- {paid_in}")
        .replace("Statement balance   $25.00", f"Statement balance   {printed}")
        .replace("$25.00   March 30", f"{printed}   March 30"),
    )
    ledger.derive()

    (row,) = rows(ledger)

    assert row.amount_due == Decimal(printed.replace("$", ""))
    assert (row.remaining, row.threshold_remaining) == (0, 0)
    assert row.status == "paid"


def test_a_newer_statement_supersedes_the_older_and_never_doubles_the_carried_balance(
    ledger: Ledger,
) -> None:
    visa(ledger)
    # The newer one first: the order of filing is not what makes it newer.
    ledger.file(NEWER_VISA_STATEMENT, fixture("vancity_visa_statement_no_minimum.txt"))
    ledger.file(VISA_STATEMENT, fixture("vancity_visa_statement.txt"))
    ledger.derive()

    older, newer = rows(ledger)

    assert (older.period_label, newer.period_label) == (
        "card-2026-01-05",
        "card-2026-03-05",
    )
    assert (older.status, newer.status) == ("superseded", "open")
    assert newer.supersedes_id == older.id
    assert older.supersedes_id is None
    assert owed_total(ledger) == newer.amount_due == Decimal("25.00")


def test_a_chain_of_three_statements_owes_only_the_newest(ledger: Ledger) -> None:
    visa(ledger)
    ledger.file(VISA_STATEMENT, fixture("vancity_visa_statement.txt"))
    ledger.file(
        "vancity-visa/2026-02-05.pdf",
        fixture("vancity_visa_statement_no_minimum.txt")
        .replace("February 6 to March 5", "January 6 to February 5")
        .replace("March 30", "February 28")
        .replace("FEB 10   FEB 11", "JAN 10   JAN 11")
        .replace("FEB 20   FEB 20", "JAN 20   JAN 20"),
    )
    ledger.file(NEWER_VISA_STATEMENT, fixture("vancity_visa_statement_no_minimum.txt"))
    ledger.derive()

    first, second, third = rows(ledger)

    assert [row.status for row in (first, second, third)] == [
        "superseded",
        "superseded",
        "open",
    ]
    assert (second.supersedes_id, third.supersedes_id) == (first.id, second.id)


def test_two_different_statements_of_one_closing_date_are_one_obligation(
    ledger: Ledger,
) -> None:
    visa(ledger)
    statement = fixture("vancity_visa_statement_no_minimum.txt")
    ledger.file("vancity-visa/a.pdf", statement)
    ledger.file(
        "vancity-visa/b.pdf",
        statement.replace(
            "Payments & credits   -   $400.00", "Payments & credits   -   $425.00"
        )
        .replace("- $400.00", "- $425.00")
        .replace("Statement balance   $25.00", "Statement balance   $0.00")
        .replace("$25.00   March 30", "$0.00   March 30"),
    )

    out = ledger.derive()

    assert out.documents == {"parsed": 2}
    (row,) = rows(ledger)
    assert (row.period_label, row.primary_source) == (
        "card-2026-03-05",
        "vancity-visa/b.pdf",
    )


def test_two_cards_are_two_payees_and_neither_supersedes_the_other(
    ledger: Ledger,
) -> None:
    accounts(
        ledger,
        ("visa", "Vancity", "credit_card", CARD, ""),
        ("mbna-card", "MBNA", "credit_card", MBNA_CARD, ""),
    )
    ledger.file(VISA_STATEMENT, fixture("vancity_visa_statement.txt"))
    ledger.file("mbna/2025-09-20.pdf", fixture("mbna_statement.txt"))
    ledger.derive()

    assert [row.status for row in rows(ledger)] == ["open", "open"]


# --- an unrecognised subscription charge ----------------------------------------------


def test_a_subscription_charge_on_a_statement_creates_no_obligation(
    ledger: Ledger,
) -> None:
    visa(ledger)
    # A vendor the owner registered, and its charge on the card.
    ledger.manual(PAYEES_FILE, "cloud,Cloud Vendor,vendor,SAMPLE PHARMACY,,")
    ledger.file(NEWER_VISA_STATEMENT, fixture("vancity_visa_statement_no_minimum.txt"))
    ledger.derive()

    (row,) = rows(ledger)

    assert row.kind == "card_statement"
    assert row.payee_id == payee_id(ledger, "visa")


def test_a_card_export_alone_creates_no_obligation(ledger: Ledger) -> None:
    visa(ledger)
    ledger.manual(PAYEES_FILE, "cafe,A Cafe,vendor,SAMPLE CAFE,,")
    ledger.file("vancity-visa/2024-02-21.csv", fixture("vancity_visa_export.csv"))
    out = ledger.derive()

    assert out.documents == {"parsed": 1}
    assert ledger.txns() != []
    assert rows(ledger) == []


# --- path (2): parsed bills -----------------------------------------------------------


def test_a_virgin_bill_is_labelled_by_its_due_month_and_issued_on_its_bill_date(
    ledger: Ledger,
) -> None:
    virgin_account(ledger)
    ledger.file(VIRGIN, fixture("virgin_bill.txt"))
    ledger.derive()

    (row,) = rows(ledger)

    assert (row.kind, row.period_label) == ("bill", "bill-2026-02")
    assert row.issued_on == D(2026, 2, 2)
    assert row.statement_date is None
    assert row.due_date == D(2026, 2, 23)
    assert row.amount_due == row.threshold_due == Decimal("126.30")
    assert row.minimum_due is None
    assert (row.paid, row.remaining, row.threshold_remaining) == (
        0,
        Decimal("126.30"),
        Decimal("126.30"),
    )
    assert (row.status, row.primary_source) == ("open", VIRGIN)
    assert row.payee_id == payee_id(ledger, "virgin-plus")
    assert duplicate_months(ledger) == {}


def test_a_fortisbc_bill_due_the_month_after_its_date_takes_the_due_month(
    ledger: Ledger,
) -> None:
    fortis_account(ledger)
    ledger.file(FORTIS, fixture("fortisbc_bill.txt"))
    ledger.derive()

    (row,) = rows(ledger)

    assert row.period_label == "bill-2026-02"
    # The bill date, not the billing period's end, which comes days before it.
    assert row.issued_on == D(2026, 1, 15)
    assert row.due_date == D(2026, 2, 5)
    assert row.amount_due == Decimal("397.60")
    assert row.payee_id == payee_id(ledger, "fortisbc")


def test_a_second_bill_of_a_due_month_takes_the_suffix_in_bill_date_order(
    ledger: Ledger,
) -> None:
    virgin_account(ledger)
    # The later bill is filed first, and its path sorts first.
    ledger.file(
        "virgin-plus/a-later.pdf",
        virgin_bill("February 9, 2026", "Feb 27, 2026", "February 27, 2026"),
    )
    ledger.file("virgin-plus/b-earlier.pdf", virgin_bill())
    ledger.file(
        "virgin-plus/c-next.pdf",
        virgin_bill("March 2, 2026", "Mar 23, 2026", "March 23, 2026"),
    )
    ledger.derive()

    first, second, third = rows(ledger)

    assert (first.period_label, first.primary_source) == (
        "bill-2026-02",
        "virgin-plus/b-earlier.pdf",
    )
    assert (second.period_label, second.primary_source) == (
        "bill-2026-02-2",
        "virgin-plus/a-later.pdf",
    )
    assert third.period_label == "bill-2026-03"
    assert duplicate_months(ledger) == {"virgin-plus": 1}


def test_a_newer_bill_supersedes_the_older_and_only_it_is_owed(ledger: Ledger) -> None:
    fortis_account(ledger)
    ledger.file("fortisbc/a.pdf", fortis_bill("February 14, 2026", "March 7, 2026"))
    ledger.file("fortisbc/b.pdf", fortis_bill("January 15, 2026", "February 5, 2026"))
    ledger.derive()

    older, newer = rows(ledger)

    assert (older.period_label, newer.period_label) == (
        "bill-2026-02",
        "bill-2026-03",
    )
    assert (older.status, newer.status) == ("superseded", "open")
    assert newer.supersedes_id == older.id
    assert owed_total(ledger) == Decimal("397.60")


def test_two_bills_with_one_bill_date_and_due_month_still_owe_one(
    ledger: Ledger,
) -> None:
    """A reissued bill prints another total on the same dates: not a copy, so it is
    numbered, and the two never add up."""
    virgin_account(ledger)
    ledger.file("virgin-plus/a.pdf", virgin_bill(total="126.30"))
    ledger.file("virgin-plus/b.pdf", virgin_bill(total="130.00"))
    ledger.derive()

    first, second = rows(ledger)

    assert (first.period_label, second.period_label) == (
        "bill-2026-02",
        "bill-2026-02-2",
    )
    assert first.status == "superseded"
    assert owed_total(ledger) == Decimal("130.00")
    assert duplicate_months(ledger) == {"virgin-plus": 1}


def test_a_copy_of_a_bill_makes_no_second_obligation(ledger: Ledger) -> None:
    virgin_account(ledger)
    ledger.file("virgin-plus/a.pdf", fixture("virgin_bill.txt"))
    ledger.file("virgin-plus/b.pdf", fixture("virgin_bill.txt") + "\n")
    out = ledger.derive()

    assert out.documents == {"parsed": 1, "superseded": 1}
    (row,) = rows(ledger)
    assert row.period_label == "bill-2026-02"
    assert duplicate_months(ledger) == {}


def test_a_bill_that_prints_no_total_makes_no_obligation(ledger: Ledger) -> None:
    virgin_account(ledger)
    ledger.file(
        VIRGIN,
        "\n".join(
            line
            for line in fixture("virgin_bill.txt").splitlines()
            if "Total amount due" not in line
        ),
    )
    out = ledger.derive()

    assert out.documents == {"parsed": 1}
    assert rows(ledger) == []


# --- the payee and the key ------------------------------------------------------------


def test_a_card_with_patterns_is_owed_to_its_account_backed_payee(
    ledger: Ledger,
) -> None:
    visa(ledger, "SAMPLE VISA PAYMENT")
    ledger.file(VISA_STATEMENT, fixture("vancity_visa_statement.txt"))
    ledger.derive()

    (row,) = rows(ledger)

    assert row.payee_id == payee_id(ledger, "visa")


def test_a_bill_account_with_patterns_is_owed_to_that_payee_not_the_issuer(
    ledger: Ledger,
) -> None:
    virgin_account(ledger, "SAMPLE MOBILE CO")
    ledger.file(VIRGIN, fixture("virgin_bill.txt"))
    ledger.derive()

    (row,) = rows(ledger)

    assert row.payee_id == payee_id(ledger, "mobile")


def test_a_payee_row_that_takes_the_issuers_slug_holds_its_bills(
    ledger: Ledger,
) -> None:
    virgin_account(ledger)
    ledger.manual(PAYEES_FILE, "virgin-plus,My phone,vendor,,,")
    ledger.file(VIRGIN, fixture("virgin_bill.txt"))
    ledger.derive()

    (row,) = rows(ledger)

    assert row.payee_id == payee_id(ledger, "virgin-plus")
    label = ledger.conn.scalar(
        select(payee.c.display_label).where(payee.c.id == row.payee_id)
    )
    assert label == "My phone"


def test_a_payee_row_that_takes_a_cards_slug_holds_its_statements(
    ledger: Ledger,
) -> None:
    visa(ledger)
    ledger.manual(PAYEES_FILE, "visa,My card,vendor,,,")
    ledger.file(VISA_STATEMENT, fixture("vancity_visa_statement.txt"))
    ledger.derive()

    (row,) = rows(ledger)

    assert row.payee_id == payee_id(ledger, "visa")


def test_the_id_comes_from_the_natural_key_alone(ledger: Ledger) -> None:
    """Superseding the row changes its figures' standing and not its id, so a decision
    that names it still finds it."""
    fortis_account(ledger)
    ledger.file("fortisbc/a.pdf", fortis_bill("January 15, 2026", "February 5, 2026"))
    ledger.derive()
    (alone,) = rows(ledger)
    fortis = payee_id(ledger, "fortisbc")
    assert alone.id == obligation_id(fortis, "bill", "bill-2026-02")  # type: ignore[arg-type]

    ledger.file("fortisbc/b.pdf", fortis_bill("February 14, 2026", "March 7, 2026"))
    ledger.derive()

    older, _ = rows(ledger)
    assert (older.id, older.status) == (alone.id, "superseded")


def test_a_second_derive_gives_the_same_rows(ledger: Ledger) -> None:
    visa(ledger)
    ledger.file(VISA_STATEMENT, fixture("vancity_visa_statement.txt"))
    ledger.derive()
    before = rows(ledger)

    ledger.derive()

    assert rows(ledger) == before
