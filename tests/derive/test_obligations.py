"""Stage 14, the five paths: card statements, parsed bills, manual bills, arrangements
and arrears, manual debts (FR-19, FR-21; Key decision 20; edge case 66; AC-87).

Seam: ``derive.run`` through the derive ``ledger`` over the synthetic statement and bill
layouts, read through the real parsers. Newer documents are the same fixtures with
invented dates and amounts written in. Every value is synthetic.
"""

from __future__ import annotations

import ast
import datetime as dt
import uuid
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import Row, select

from rheo_finance.derive.stage14 import NoPayee, obligation_id
from rheo_finance.manual.accounts import ACCOUNTS_FILE, IDENTIFIERS_FILE
from rheo_finance.manual.arrangements import ARRANGEMENTS_FILE
from rheo_finance.manual.obligations import OBLIGATIONS_FILE
from rheo_finance.manual.payees import PAYEES_FILE
from rheo_finance.reports.views import (
    DUPLICATE_BILL_MONTH,
    MANUAL_MISMATCH,
    STATEMENT_CONFLICT,
    TYPED_BILL_UNJOINED,
)
from rheo_finance.storage.schema import document, obligation, payee, report_status

from .support import FIXTURES, Ledger

D = dt.date
CARD = "9999 99** **** 9991"
MBNA_CARD = "9999 99XX XXXX 9992"
VISA_STATEMENT = "vancity-visa/2026-01-05.pdf"
NEWER_VISA_STATEMENT = "vancity-visa/2026-03-05.pdf"
VIRGIN = "virgin-plus/bill.pdf"
FORTIS = "fortisbc/bill.pdf"
SRC = Path(__file__).resolve().parents[2] / "src" / "rheo_finance"
SECOND_VIRGIN_NUMBER = "999000456"
ARREARS_LINE = "   Arrears   $100.00\n"


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


def payee_id(ledger: Ledger, slug: str) -> uuid.UUID:
    found: uuid.UUID = ledger.conn.execute(
        select(payee.c.id).where(payee.c.slug == slug)
    ).scalar_one()
    return found


def owed_total(ledger: Ledger) -> Decimal:
    return sum(
        (Decimal(row.remaining) for row in rows(ledger) if row.status != "superseded"),
        Decimal(0),
    )


def facts(ledger: Ledger, name: str) -> dict[str | None, int]:
    found = ledger.conn.execute(
        select(report_status.c.subject, report_status.c.count).where(
            report_status.c.fact == name
        )
    ).all()
    return {subject: count for subject, count in found}


def duplicate_months(ledger: Ledger) -> dict[str | None, int]:
    return facts(ledger, DUPLICATE_BILL_MONTH)


def by_label(ledger: Ledger) -> dict[str, Row[tuple[object, ...]]]:
    return {str(row.period_label): row for row in rows(ledger)}


def sample_internet(ledger: Ledger) -> None:
    ledger.manual(PAYEES_FILE, "sample-internet,Sample Internet,vendor,,,")


def virgin_with_arrears(text: str) -> str:
    return text.replace(
        "   Unpaid balance   $45.10\n",
        "   Unpaid balance   $45.10\n   Arrears   $30.00\n",
    )


def fortis_with_arrears(text: str) -> str:
    return text.replace(
        "   Balance forward   $212.18\n",
        f"   Balance forward   $212.18\n{ARREARS_LINE}",
    )


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


def zero_balance_statement() -> str:
    return (
        fixture("vancity_visa_statement_no_minimum.txt")
        .replace("Payments & credits   -   $400.00", "Payments & credits   -   $425.00")
        .replace("- $400.00", "- $425.00")
        .replace("Statement balance   $25.00", "Statement balance   $0.00")
        .replace("$25.00   March 30", "$0.00   March 30")
    )


@pytest.mark.parametrize(
    ("first", "second"),
    [("vancity-visa/a.pdf", "vancity-visa/b.pdf"),
     ("vancity-visa/b.pdf", "vancity-visa/a.pdf")],
    ids=["one-way", "the-other-way"],
)  # fmt: skip
def test_two_statements_of_one_closing_date_with_other_figures_are_one_row_and_a_fact(
    ledger: Ledger, first: str, second: str
) -> None:
    """The one ``precedence`` ranks first holds the row, here the larger sha256: which
    path sorts last does not decide it."""
    visa(ledger)
    ledger.file(first, fixture("vancity_visa_statement_no_minimum.txt"))
    ledger.file(second, zero_balance_statement())

    out = ledger.derive()

    assert out.documents == {"parsed": 2}
    (row,) = rows(ledger)
    assert row.period_label == "card-2026-03-05"
    winner = ledger.conn.execute(
        select(document.c.path).order_by(document.c.sha256.desc()).limit(1)
    ).scalar_one()
    assert row.primary_source == winner
    assert row.statement_conflict is True
    assert facts(ledger, STATEMENT_CONFLICT) == {"Visa": 1}


def test_two_statements_of_one_closing_date_with_the_same_figures_record_no_fact(
    ledger: Ledger,
) -> None:
    """Their lines differ, so neither is a copy of the other; what is owed does not."""
    visa(ledger)
    statement = fixture("vancity_visa_statement_no_minimum.txt")
    ledger.file("vancity-visa/a.pdf", statement)
    ledger.file(
        "vancity-visa/b.pdf", statement.replace("SAMPLE PHARMACY", "SAMPLE CHEMIST")
    )

    out = ledger.derive()

    assert out.documents == {"parsed": 2}
    (row,) = rows(ledger)
    assert row.statement_conflict is False
    assert facts(ledger, STATEMENT_CONFLICT) == {}


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


def test_a_statement_on_an_account_not_registered_as_a_card_fails_the_derive(
    ledger: Ledger,
) -> None:
    """Dropping it would hide what the statement says is owed."""
    accounts(ledger, ("visa", "Vancity", "chequing", "999000009991", ""))
    ledger.file(VISA_STATEMENT, fixture("vancity_visa_statement.txt"))

    with pytest.raises(NoPayee, match="account visa"):
        ledger.derive()


def test_a_payee_row_gives_a_statement_on_a_misregistered_card_its_payee(
    ledger: Ledger,
) -> None:
    accounts(ledger, ("visa", "Vancity", "chequing", "999000009991", ""))
    ledger.manual(PAYEES_FILE, "visa,My card,vendor,,,")
    ledger.file(VISA_STATEMENT, fixture("vancity_visa_statement.txt"))
    ledger.derive()

    (row,) = rows(ledger)

    assert row.kind == "card_statement"
    assert row.payee_id == payee_id(ledger, "visa")


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
    assert alone.id == obligation_id(fortis, "bill", "bill-2026-02")

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


# --- supersession is per account ------------------------------------------------------


def two_virgin_accounts(ledger: Ledger) -> None:
    accounts(
        ledger,
        ("mobile", "Virgin Plus", "bill_vendor", "999000123", ""),
        ("second-mobile", "Virgin Plus", "bill_vendor", SECOND_VIRGIN_NUMBER, ""),
    )


def second_account(text: str) -> str:
    return text.replace("999000123", SECOND_VIRGIN_NUMBER)


def test_two_accounts_of_one_issuer_do_not_supersede_each_other(ledger: Ledger) -> None:
    two_virgin_accounts(ledger)
    ledger.file("virgin-plus/a.pdf", virgin_bill())
    ledger.file(
        "virgin-plus/b.pdf",
        second_account(virgin_bill("March 2, 2026", "Mar 23, 2026", "March 23, 2026")),
    )
    ledger.derive()

    first, second = rows(ledger)

    assert (first.period_label, second.period_label) == (
        "bill-2026-02",
        "bill-2026-03",
    )
    assert (first.status, second.status) == ("open", "open")
    assert second.supersedes_id is None
    assert first.payee_id == second.payee_id == payee_id(ledger, "virgin-plus")


def test_each_account_of_one_issuer_keeps_its_own_chain(ledger: Ledger) -> None:
    two_virgin_accounts(ledger)
    ledger.file("virgin-plus/a.pdf", virgin_bill())
    ledger.file("virgin-plus/b.pdf", second_account(virgin_bill()))
    ledger.file(
        "virgin-plus/c.pdf",
        virgin_bill("March 2, 2026", "Mar 23, 2026", "March 23, 2026"),
    )
    ledger.derive()

    found = by_label(ledger)

    # One due month across two accounts is one payee's second bill of that month.
    assert sorted(found) == ["bill-2026-02", "bill-2026-02-2", "bill-2026-03"]
    assert found["bill-2026-03"].supersedes_id == found["bill-2026-02"].id
    assert found["bill-2026-02"].status == "superseded"
    assert found["bill-2026-02-2"].status == "open"
    assert found["bill-2026-03"].status == "open"
    assert duplicate_months(ledger) == {"virgin-plus": 1}


# --- path (3): manual bills -----------------------------------------------------------


def test_a_manual_bill_is_one_obligation_with_its_row_as_its_source(
    ledger: Ledger,
) -> None:
    sample_internet(ledger)
    ledger.manual(
        OBLIGATIONS_FILE,
        "sample-internet,bill,,SI-999,64.50,,2026-02-20,,2026-02-01",
    )
    ledger.derive()

    (row,) = rows(ledger)

    assert (row.kind, row.period_label) == ("bill", "bill-2026-02")
    assert row.issued_on == D(2026, 2, 1)
    assert row.due_date == D(2026, 2, 20)
    assert row.amount_due == row.threshold_due == row.remaining == Decimal("64.50")
    assert row.status == "open"
    assert row.primary_source == row.manual_source == "manual:obligations.csv:2"
    assert row.manual_mismatch is None


def test_a_bills_written_minimum_is_kept_and_is_not_its_threshold(
    ledger: Ledger,
) -> None:
    sample_internet(ledger)
    ledger.manual(
        OBLIGATIONS_FILE,
        "sample-internet,bill,,,64.50,10.00,2026-02-20,,2026-02-01",
    )
    ledger.derive()

    (row,) = rows(ledger)

    assert row.minimum_due == Decimal("10.00")
    assert row.threshold_due == row.threshold_remaining == Decimal("64.50")


def test_the_raw_invoice_email_a_row_names_is_its_source_and_makes_nothing(
    ledger: Ledger,
) -> None:
    sample_internet(ledger)
    ledger.file("_mail/invoice.eml", "From: billing@example.test\n\nSample invoice\n")
    ledger.file("_mail/other.eml", "From: billing@example.test\n\nAnother invoice\n")
    ledger.manual(
        OBLIGATIONS_FILE,
        "sample-internet,bill,,,64.50,,2026-02-20,_mail/invoice.eml,2026-02-01",
    )
    ledger.derive()

    (row,) = rows(ledger)

    assert row.primary_source == "_mail/invoice.eml"
    assert row.manual_source == "manual:obligations.csv:2"


def test_a_row_naming_a_path_with_no_document_keeps_the_row_as_its_source(
    ledger: Ledger,
) -> None:
    sample_internet(ledger)
    ledger.manual(
        OBLIGATIONS_FILE,
        "sample-internet,bill,,,64.50,,2026-02-20,_mail/never-filed.eml,2026-02-01",
    )
    ledger.derive()

    (row,) = rows(ledger)

    assert row.primary_source == "manual:obligations.csv:2"


def test_invoice_style_manual_bills_do_not_chain_so_two_of_one_payee_are_both_owed(
    ledger: Ledger,
) -> None:
    """A payee with no bill source: an invoice does not carry the one before it. This is
    how two live obligations of one payee arise for the payment matrix (AC-25)."""
    sample_internet(ledger)
    ledger.manual(
        OBLIGATIONS_FILE,
        "sample-internet,bill,,,64.50,,2026-02-20,,2026-02-01",
        "sample-internet,bill,,,70.00,,2026-03-20,,2026-03-01",
    )
    ledger.derive()

    first, second = rows(ledger)

    assert (first.status, second.status) == ("open", "open")
    assert second.supersedes_id is None
    assert owed_total(ledger) == Decimal("134.50")


def test_a_newer_parsed_bill_supersedes_an_older_manual_bill_of_a_bill_source(
    ledger: Ledger,
) -> None:
    """A bill typed from a photo, then the next bill as a PDF: its total carries the
    typed one, so it is owed once."""
    virgin_account(ledger)
    ledger.manual(
        OBLIGATIONS_FILE,
        "virgin-plus,bill,,,64.50,,2026-01-20,,2026-01-02",
    )
    ledger.file(VIRGIN, fixture("virgin_bill.txt"))
    ledger.derive()

    manual, parsed = rows(ledger)

    assert (manual.status, parsed.status) == ("superseded", "open")
    assert parsed.supersedes_id == manual.id
    assert owed_total(ledger) == Decimal("126.30")


def test_a_newer_manual_bill_supersedes_an_older_parsed_bill_of_a_bill_source(
    ledger: Ledger,
) -> None:
    virgin_account(ledger)
    ledger.file(VIRGIN, fixture("virgin_bill.txt"))
    ledger.manual(
        OBLIGATIONS_FILE,
        "virgin-plus,bill,,,140.00,,2026-03-23,,2026-03-02",
    )
    ledger.derive()

    parsed, manual = rows(ledger)

    assert (parsed.status, manual.status) == ("superseded", "open")
    assert manual.supersedes_id == parsed.id
    assert owed_total(ledger) == Decimal("140.00")


def test_a_manual_bill_takes_its_place_in_the_chain_by_issue_date(
    ledger: Ledger,
) -> None:
    virgin_account(ledger)
    ledger.file("virgin-plus/a.pdf", virgin_bill())
    ledger.file(
        "virgin-plus/c.pdf",
        virgin_bill("April 2, 2026", "Apr 23, 2026", "April 23, 2026"),
    )
    ledger.manual(
        OBLIGATIONS_FILE,
        "virgin-plus,bill,,,140.00,,2026-03-23,,2026-03-02",
    )
    ledger.derive()

    first, typed, last = rows(ledger)

    assert (first.status, typed.status, last.status) == (
        "superseded",
        "superseded",
        "open",
    )
    assert (typed.supersedes_id, last.supersedes_id) == (first.id, typed.id)


def test_two_bills_typed_in_for_an_issuer_with_no_parsed_bill_owe_the_newer(
    ledger: Ledger,
) -> None:
    """An issuer's photographed bills: 400 then 600, and the 600 holds the 400."""
    ledger.manual(
        OBLIGATIONS_FILE,
        "fortisbc,bill,,,400.00,,2026-02-05,,2026-01-15",
        "fortisbc,bill,,,600.00,,2026-03-05,,2026-02-15",
    )
    ledger.derive()

    older, newer = rows(ledger)

    assert (older.status, newer.status) == ("superseded", "open")
    assert newer.supersedes_id == older.id
    assert owed_total(ledger) == Decimal("600.00")


def test_two_bills_typed_in_for_a_bill_vendor_account_owe_the_newer(
    ledger: Ledger,
) -> None:
    virgin_account(ledger, "SAMPLE MOBILE CO")
    ledger.manual(
        OBLIGATIONS_FILE,
        "mobile,bill,,,400.00,,2026-02-05,,2026-01-15",
        "mobile,bill,,,600.00,,2026-03-05,,2026-02-15",
    )
    ledger.derive()

    assert owed_total(ledger) == Decimal("600.00")


def test_a_manual_bill_of_an_issuer_with_two_billed_accounts_joins_neither(
    ledger: Ledger,
) -> None:
    """Nothing says which account it is on, so it supersedes neither."""
    two_virgin_accounts(ledger)
    ledger.file("virgin-plus/a.pdf", virgin_bill())
    ledger.file("virgin-plus/b.pdf", second_account(virgin_bill()))
    ledger.manual(
        OBLIGATIONS_FILE,
        "virgin-plus,bill,,,140.00,,2026-03-23,,2026-03-02",
    )
    ledger.derive()

    assert [row.status for row in rows(ledger)] == ["open", "open", "open"]
    assert facts(ledger, TYPED_BILL_UNJOINED) == {"virgin-plus": 1}
    assert [row.unjoined for row in rows(ledger)].count(True) == 1


def test_a_typed_bill_that_joins_a_chain_records_no_fact(ledger: Ledger) -> None:
    virgin_account(ledger)
    ledger.file(VIRGIN, fixture("virgin_bill.txt"))
    ledger.manual(
        OBLIGATIONS_FILE,
        "virgin-plus,bill,,,140.00,,2026-03-23,,2026-03-02",
    )
    ledger.derive()

    assert facts(ledger, TYPED_BILL_UNJOINED) == {}


def test_typed_bills_of_an_issuer_with_no_parsed_bill_record_no_fact(
    ledger: Ledger,
) -> None:
    """They chain with each other, and no parsed bill stands beside them."""
    ledger.manual(
        OBLIGATIONS_FILE,
        "fortisbc,bill,,,400.00,,2026-02-05,,2026-01-15",
    )
    ledger.derive()

    assert facts(ledger, TYPED_BILL_UNJOINED) == {}


def test_a_bill_sources_manual_debts_never_chain(ledger: Ledger) -> None:
    """A debt to a bill vendor is not a bill: the newer one holds nothing of the older,
    and chaining them would make the older debt vanish from what is owed."""
    virgin_account(ledger)
    ledger.file(VIRGIN, fixture("virgin_bill.txt"))
    ledger.manual(
        OBLIGATIONS_FILE,
        "virgin-plus,manual_debt,first,,500.00,,,,2026-01-01",
        "virgin-plus,manual_debt,second,,20.00,,,,2026-03-01",
    )
    ledger.derive()

    found = by_label(ledger)

    assert (found["debt-first"].status, found["debt-second"].status) == ("open", "open")
    assert found["debt-second"].supersedes_id is None
    assert found["bill-2026-02"].status == "open"
    assert owed_total(ledger) == Decimal("646.30")


def test_a_manual_bill_chains_on_the_account_of_the_parsed_bills_not_the_stood_for_one(
    ledger: Ledger,
) -> None:
    """The issuer's payee stands for one account and its parsed bills are on the other:
    the bills on file say where the running balance is."""
    accounts(
        ledger,
        ("virgin-plus", "Virgin Plus", "bill_vendor", "999000123", "SAMPLE MOBILE CO"),
        ("second-mobile", "Virgin Plus", "bill_vendor", SECOND_VIRGIN_NUMBER, ""),
    )
    ledger.file(VIRGIN, second_account(fixture("virgin_bill.txt")))
    ledger.manual(
        OBLIGATIONS_FILE,
        "virgin-plus,bill,,,140.00,,2026-03-23,,2026-03-02",
    )
    ledger.derive()

    parsed, typed = rows(ledger)

    assert (parsed.status, typed.status) == ("superseded", "open")
    assert typed.supersedes_id == parsed.id
    assert owed_total(ledger) == Decimal("140.00")


def test_a_later_due_date_is_the_newer_of_two_bills_issued_on_one_date(
    ledger: Ledger,
) -> None:
    """The later due date is written first: the order of the rows does not decide it."""
    ledger.manual(
        OBLIGATIONS_FILE,
        "fortisbc,bill,,,600.00,,2026-03-05,,2026-01-15",
        "fortisbc,bill,,,400.00,,2026-02-05,,2026-01-15",
    )
    ledger.derive()

    found = by_label(ledger)

    assert (found["bill-2026-02"].status, found["bill-2026-03"].status) == (
        "superseded",
        "open",
    )
    assert found["bill-2026-03"].supersedes_id == found["bill-2026-02"].id
    assert owed_total(ledger) == Decimal("600.00")


# --- a bill row that names the issuer of an account with its own payee ----------------


def test_a_bill_row_naming_the_issuer_is_read_as_the_standing_payee(
    ledger: Ledger,
) -> None:
    """One bill, parsed under the account's own payee and typed under the issuer's, is
    owed once."""
    virgin_account(ledger, "SAMPLE MOBILE CO")
    ledger.file(VIRGIN, fixture("virgin_bill.txt"))
    ledger.manual(
        OBLIGATIONS_FILE,
        "virgin-plus,bill,,,126.30,,2026-02-23,,2026-02-02",
    )
    ledger.derive()

    (row,) = rows(ledger)

    assert row.payee_id == payee_id(ledger, "mobile")
    assert row.primary_source == VIRGIN
    assert row.manual_source == "manual:obligations.csv:2"
    assert owed_total(ledger) == Decimal("126.30")
    assert facts(ledger, TYPED_BILL_UNJOINED) == {}


def test_a_later_bill_row_naming_the_issuer_chains_under_the_standing_payee(
    ledger: Ledger,
) -> None:
    virgin_account(ledger, "SAMPLE MOBILE CO")
    ledger.file(VIRGIN, fixture("virgin_bill.txt"))
    ledger.manual(
        OBLIGATIONS_FILE,
        "virgin-plus,bill,,,140.00,,2026-03-23,,2026-03-02",
    )
    ledger.derive()

    parsed, typed = rows(ledger)

    assert typed.payee_id == parsed.payee_id == payee_id(ledger, "mobile")
    assert (parsed.status, typed.status) == ("superseded", "open")
    assert owed_total(ledger) == Decimal("140.00")


def test_a_debt_row_naming_the_issuer_stays_on_the_issuers_payee(
    ledger: Ledger,
) -> None:
    """A debt is not one of the account's bills."""
    virgin_account(ledger, "SAMPLE MOBILE CO")
    ledger.file(VIRGIN, fixture("virgin_bill.txt"))
    ledger.manual(OBLIGATIONS_FILE, "virgin-plus,manual_debt,loan,,500.00,,,,")
    ledger.derive()

    assert by_label(ledger)["debt-loan"].payee_id == payee_id(ledger, "virgin-plus")


def test_a_bill_row_naming_an_issuer_with_no_parsed_bill_stays_where_it_was_written(
    ledger: Ledger,
) -> None:
    virgin_account(ledger, "SAMPLE MOBILE CO")
    ledger.manual(
        OBLIGATIONS_FILE,
        "virgin-plus,bill,,,126.30,,2026-02-23,,2026-02-02",
    )
    ledger.derive()

    (row,) = rows(ledger)

    assert row.payee_id == payee_id(ledger, "virgin-plus")
    assert facts(ledger, TYPED_BILL_UNJOINED) == {}


def test_a_bill_row_naming_an_issuer_billed_under_two_payees_stays_and_is_counted(
    ledger: Ledger,
) -> None:
    """Either account's bill could be the one it copies: it is not guessed, and status
    says so."""
    accounts(
        ledger,
        ("mobile", "Virgin Plus", "bill_vendor", "999000123", "SAMPLE MOBILE CO"),
        (
            "second-mobile",
            "Virgin Plus",
            "bill_vendor",
            SECOND_VIRGIN_NUMBER,
            "SAMPLE SECOND CO",
        ),
    )
    ledger.file("virgin-plus/a.pdf", virgin_bill())
    ledger.file("virgin-plus/b.pdf", second_account(virgin_bill()))
    ledger.manual(
        OBLIGATIONS_FILE,
        "virgin-plus,bill,,,126.30,,2026-02-23,,2026-02-02",
    )
    ledger.derive()

    typed = [
        row for row in rows(ledger) if row.payee_id == payee_id(ledger, "virgin-plus")
    ]

    assert len(typed) == 1
    assert len(rows(ledger)) == 3
    assert facts(ledger, TYPED_BILL_UNJOINED) == {"virgin-plus": 1}


# --- a manual row and a parsed document -----------------------------------------------


def test_a_manual_row_and_a_parsed_bill_of_one_key_are_one_with_the_documents_figures(
    ledger: Ledger,
) -> None:
    virgin_account(ledger)
    ledger.manual(
        OBLIGATIONS_FILE,
        "virgin-plus,bill,,,120.00,,2026-02-28,,2026-02-01",
    )
    ledger.file(VIRGIN, fixture("virgin_bill.txt"))
    ledger.derive()

    (row,) = rows(ledger)

    assert row.amount_due == row.remaining == Decimal("126.30")
    assert row.issued_on == D(2026, 2, 2)
    assert row.due_date == D(2026, 2, 23)
    assert row.primary_source == VIRGIN
    assert row.manual_source == "manual:obligations.csv:2"
    assert row.manual_mismatch == Decimal("-6.30")
    assert facts(ledger, MANUAL_MISMATCH) == {"virgin-plus": 1}


def test_a_manual_row_that_agrees_with_its_document_records_no_mismatch(
    ledger: Ledger,
) -> None:
    virgin_account(ledger)
    ledger.manual(
        OBLIGATIONS_FILE,
        "virgin-plus,bill,,,126.30,,2026-02-23,,2026-02-02",
    )
    ledger.file(VIRGIN, fixture("virgin_bill.txt"))
    ledger.derive()

    (row,) = rows(ledger)

    assert row.manual_source == "manual:obligations.csv:2"
    assert row.manual_mismatch is None
    assert facts(ledger, MANUAL_MISMATCH) == {}


def test_a_manual_row_merges_with_the_second_bill_of_a_month_by_its_label(
    ledger: Ledger,
) -> None:
    virgin_account(ledger)
    ledger.file("virgin-plus/a.pdf", virgin_bill())
    ledger.file(
        "virgin-plus/b.pdf",
        virgin_bill("February 9, 2026", "Feb 27, 2026", "February 27, 2026"),
    )
    ledger.manual(
        OBLIGATIONS_FILE,
        "virgin-plus,bill,bill-2026-02-2,,126.30,,2026-02-27,,2026-02-09",
    )
    ledger.derive()

    found = by_label(ledger)

    assert sorted(found) == ["bill-2026-02", "bill-2026-02-2"]
    assert found["bill-2026-02"].manual_source is None
    assert found["bill-2026-02-2"].manual_source == "manual:obligations.csv:2"


def test_a_merged_row_still_chains_as_its_document_does(ledger: Ledger) -> None:
    virgin_account(ledger)
    ledger.manual(
        OBLIGATIONS_FILE,
        "virgin-plus,bill,,,126.30,,2026-02-23,,2026-02-02",
    )
    ledger.file("virgin-plus/a.pdf", virgin_bill())
    ledger.file(
        "virgin-plus/b.pdf",
        virgin_bill("March 2, 2026", "Mar 23, 2026", "March 23, 2026"),
    )
    ledger.derive()

    merged, newer = rows(ledger)

    assert (merged.status, newer.status) == ("superseded", "open")
    assert newer.supersedes_id == merged.id
    assert owed_total(ledger) == Decimal("126.30")


# --- path (4): arrangements and arrears -----------------------------------------------


def arrangement(ledger: Ledger, payee_slug: str, replaces: str = "") -> None:
    ledger.manual(
        ARRANGEMENTS_FILE,
        f"plan,{payee_slug},2026-01-10,1,50.00,2026-04-01,{replaces}",
        f"plan,{payee_slug},2026-01-10,2,60.00,2026-05-01,{replaces}",
    )


def test_each_instalment_is_an_obligation_issued_when_the_arrangement_was_agreed(
    ledger: Ledger,
) -> None:
    sample_internet(ledger)
    arrangement(ledger, "sample-internet")
    ledger.derive()

    first, second = rows(ledger)

    assert (first.kind, first.period_label) == (
        "arrangement_instalment",
        "inst-plan-1",
    )
    assert second.period_label == "inst-plan-2"
    assert first.issued_on == second.issued_on == D(2026, 1, 10)
    assert first.due_date == D(2026, 4, 1)
    assert (
        first.amount_due == first.threshold_due == first.remaining == Decimal("50.00")
    )
    assert (first.status, first.arrangement_ref) == ("open", "plan")
    assert first.primary_source == "manual:arrangements.csv:2"
    assert first.manual_source is None
    assert owed_total(ledger) == Decimal("110.00")


def test_an_active_arrangement_supersedes_what_it_replaces_and_owes_current_charges(
    ledger: Ledger,
) -> None:
    fortis_account(ledger)
    arrangement(ledger, "fortisbc", "bill-2026-02")
    ledger.file("fortisbc/a.pdf", fortis_bill("January 15, 2026", "February 5, 2026"))
    ledger.file("fortisbc/b.pdf", fortis_bill("February 14, 2026", "March 7, 2026"))
    ledger.derive()

    found = by_label(ledger)

    replaced, current = found["bill-2026-02"], found["bill-2026-03"]
    assert (replaced.status, replaced.arrangement_ref) == ("superseded", "plan")
    assert current.status == "open"
    assert current.arrangement_ref is None
    # The total printed holds what the instalments now cover.
    assert current.amount_due == current.remaining == Decimal("185.42")
    assert owed_total(ledger) == Decimal("295.42")


def test_a_replaced_newest_bill_leaves_only_the_instalments_owed(
    ledger: Ledger,
) -> None:
    fortis_account(ledger)
    arrangement(ledger, "fortisbc", "bill-2026-02")
    ledger.file(FORTIS, fixture("fortisbc_bill.txt"))
    ledger.derive()

    assert by_label(ledger)["bill-2026-02"].status == "superseded"
    assert owed_total(ledger) == Decimal("110.00")


def test_an_arrangement_replaces_a_manual_bill_too(ledger: Ledger) -> None:
    sample_internet(ledger)
    arrangement(ledger, "sample-internet", "bill-2026-02")
    ledger.manual(
        OBLIGATIONS_FILE,
        "sample-internet,bill,,,64.50,,2026-02-20,,2026-02-01",
        "sample-internet,bill,,,70.00,,2026-03-20,,2026-03-01",
    )
    ledger.derive()

    found = by_label(ledger)

    assert found["bill-2026-02"].status == "superseded"
    assert found["bill-2026-03"].status == "open"
    assert found["bill-2026-03"].amount_due == Decimal("70.00")


def test_a_manual_row_is_compared_with_the_total_printed_not_the_current_charges(
    ledger: Ledger,
) -> None:
    fortis_account(ledger)
    arrangement(ledger, "fortisbc")
    ledger.manual(
        OBLIGATIONS_FILE,
        "fortisbc,bill,,,397.60,,2026-02-05,,2026-01-15",
    )
    ledger.file(FORTIS, fixture("fortisbc_bill.txt"))
    ledger.derive()

    bill = by_label(ledger)["bill-2026-02"]

    assert bill.amount_due == Decimal("185.42")
    assert bill.manual_mismatch is None


def test_an_arrangement_of_another_payee_changes_nothing(ledger: Ledger) -> None:
    fortis_account(ledger)
    sample_internet(ledger)
    arrangement(ledger, "sample-internet", "bill-2026-02")
    ledger.file(FORTIS, fortis_with_arrears(fixture("fortisbc_bill.txt")))
    ledger.derive()

    found = by_label(ledger)

    assert found["bill-2026-02"].status == "open"
    assert found["bill-2026-02"].amount_due == Decimal("297.60")
    assert found["arrears-2026-01"].amount_due == Decimal("100.00")


def test_a_bill_with_an_arrears_line_splits_and_still_adds_up_to_its_total(
    ledger: Ledger,
) -> None:
    fortis_account(ledger)
    ledger.file(FORTIS, fortis_with_arrears(fixture("fortisbc_bill.txt")))
    ledger.derive()

    found = by_label(ledger)

    arrears, bill = found["arrears-2026-01"], found["bill-2026-02"]
    assert arrears.kind == "arrears"
    # The as-of date is the bill's, and it is due when the bill is.
    assert arrears.issued_on == D(2026, 1, 15)
    assert arrears.due_date == bill.due_date == D(2026, 2, 5)
    assert arrears.amount_due == arrears.threshold_due == Decimal("100.00")
    assert (arrears.status, arrears.primary_source) == ("open", FORTIS)
    assert bill.amount_due == Decimal("297.60")
    assert owed_total(ledger) == Decimal("397.60")


def test_a_virgin_bill_with_an_arrears_line_splits_too(ledger: Ledger) -> None:
    virgin_account(ledger)
    ledger.file(VIRGIN, virgin_with_arrears(fixture("virgin_bill.txt")))
    ledger.derive()

    found = by_label(ledger)

    assert found["arrears-2026-02"].amount_due == Decimal("30.00")
    assert found["bill-2026-02"].amount_due == Decimal("96.30")
    assert owed_total(ledger) == Decimal("126.30")


def test_a_carried_balance_with_no_arrears_line_stays_in_the_bills_own_total(
    ledger: Ledger,
) -> None:
    virgin_account(ledger)
    ledger.file(VIRGIN, fixture("virgin_bill.txt"))
    ledger.derive()

    (row,) = rows(ledger)

    assert row.kind == "bill"
    assert row.amount_due == Decimal("126.30")


def test_an_active_arrangement_suppresses_arrears_so_never_both_are_owed(
    ledger: Ledger,
) -> None:
    fortis_account(ledger)
    arrangement(ledger, "fortisbc")
    ledger.file(FORTIS, fortis_with_arrears(fixture("fortisbc_bill.txt")))
    ledger.derive()

    assert sorted(by_label(ledger)) == ["bill-2026-02", "inst-plan-1", "inst-plan-2"]
    assert by_label(ledger)["bill-2026-02"].amount_due == Decimal("185.42")
    assert owed_total(ledger) == Decimal("295.42")


def test_a_bill_printing_no_current_charges_is_owed_its_total_less_its_arrears(
    ledger: Ledger,
) -> None:
    """With an arrangement active, what it prints is all there is to go on."""
    fortis_account(ledger)
    arrangement(ledger, "fortisbc")
    no_charges = "".join(
        line
        for line in fixture("fortisbc_bill.txt").splitlines(keepends=True)
        if not line.strip().startswith("Current charges  ")
    )
    assert no_charges != fixture("fortisbc_bill.txt")
    ledger.file(FORTIS, no_charges)
    ledger.file("fortisbc/arrears.pdf", fortis_with_arrears(no_charges).replace(
        "January 15, 2026", "February 14, 2026").replace(
        "February 5, 2026", "March 7, 2026"))  # fmt: skip
    ledger.derive()

    found = by_label(ledger)

    assert found["bill-2026-02"].status == "superseded"
    assert found["bill-2026-03"].amount_due == Decimal("297.60")


def test_only_the_newest_bill_states_arrears(ledger: Ledger) -> None:
    """A later bill that prints none has folded them in or had them paid: the older
    figure is not owed on top of it."""
    fortis_account(ledger)
    ledger.file(
        "fortisbc/a.pdf",
        fortis_with_arrears(fortis_bill("January 15, 2026", "February 5, 2026")),
    )
    ledger.file("fortisbc/b.pdf", fortis_bill("February 14, 2026", "March 7, 2026"))
    ledger.derive()

    assert sorted(by_label(ledger)) == ["bill-2026-02", "bill-2026-03"]
    assert owed_total(ledger) == Decimal("397.60")


def test_arrears_of_two_accounts_of_one_issuer_are_two_rows(ledger: Ledger) -> None:
    two_virgin_accounts(ledger)
    ledger.file("virgin-plus/a.pdf", virgin_with_arrears(virgin_bill()))
    ledger.file("virgin-plus/b.pdf", second_account(virgin_with_arrears(virgin_bill())))
    ledger.derive()

    found = by_label(ledger)

    assert {"arrears-2026-02", "arrears-2026-02-2"} <= set(found)
    assert owed_total(ledger) == Decimal("252.60")


def test_an_arrangement_owes_a_virgin_bill_its_printed_current_charges(
    ledger: Ledger,
) -> None:
    virgin_account(ledger)
    arrangement(ledger, "virgin-plus")
    ledger.file(VIRGIN, fixture("virgin_bill.txt"))
    ledger.derive()

    bill = by_label(ledger)["bill-2026-02"]

    assert bill.amount_due == bill.remaining == Decimal("81.20")
    assert owed_total(ledger) == Decimal("191.20")


def test_a_typed_in_bill_beside_an_active_arrangement_is_owed_as_typed(
    ledger: Ledger,
) -> None:
    """It supersedes the parsed bill before it, which the arrangement replaced, and it
    prints no current charges to read."""
    fortis_account(ledger)
    arrangement(ledger, "fortisbc", "bill-2026-02")
    ledger.file(FORTIS, fixture("fortisbc_bill.txt"))
    ledger.manual(
        OBLIGATIONS_FILE,
        "fortisbc,bill,,,90.00,,2026-03-05,,2026-02-15",
    )
    ledger.derive()

    found = by_label(ledger)

    assert (found["bill-2026-02"].status, found["bill-2026-03"].status) == (
        "superseded",
        "open",
    )
    assert found["bill-2026-03"].amount_due == Decimal("90.00")
    assert found["bill-2026-03"].supersedes_id == found["bill-2026-02"].id
    assert owed_total(ledger) == Decimal("200.00")


def test_a_replaced_typed_in_bill_leaves_only_the_instalments_owed(
    ledger: Ledger,
) -> None:
    fortis_account(ledger)
    arrangement(ledger, "fortisbc", "bill-2026-02")
    ledger.manual(
        OBLIGATIONS_FILE,
        "fortisbc,bill,,,400.00,,2026-02-05,,2026-01-15",
    )
    ledger.derive()

    assert by_label(ledger)["bill-2026-02"].status == "superseded"
    assert owed_total(ledger) == Decimal("110.00")


def test_a_replaced_bill_keeps_its_printed_total(ledger: Ledger) -> None:
    """The arrangement rule changes what a live bill is owed, and this one is not."""
    fortis_account(ledger)
    arrangement(ledger, "fortisbc", "bill-2026-02")
    ledger.file(FORTIS, fixture("fortisbc_bill.txt"))
    ledger.derive()

    assert by_label(ledger)["bill-2026-02"].amount_due == Decimal("397.60")


def fortis_credited(
    payments: tuple[str, str], forward: str, total: str, arrears: str = ""
) -> str:
    """The FortisBC fixture with larger payments, so its total falls below its current
    charges of $185.42. The identity still holds."""
    return (
        fixture("fortisbc_bill.txt")
        .replace("-$150.00", payments[0])
        .replace("-$50.00", payments[1])
        .replace("Balance forward   $212.18", f"Balance forward   {forward}{arrears}")
        .replace("$397.60", total)
    )


def test_current_charges_above_a_credited_total_owe_only_the_total(
    ledger: Ledger,
) -> None:
    fortis_account(ledger)
    arrangement(ledger, "fortisbc")
    ledger.file(FORTIS, fortis_credited(("-$250.00", "-$300.00"), "-$137.82", "$47.60"))
    ledger.derive()

    bill = by_label(ledger)["bill-2026-02"]

    assert bill.amount_due == bill.remaining == Decimal("47.60")


def test_a_bill_in_credit_under_an_arrangement_stays_as_printed_and_owes_nothing(
    ledger: Ledger,
) -> None:
    fortis_account(ledger)
    arrangement(ledger, "fortisbc")
    ledger.file(
        FORTIS, fortis_credited(("-$400.00", "-$450.00"), "-$437.82", "-$252.40")
    )
    ledger.derive()

    bill = by_label(ledger)["bill-2026-02"]

    assert bill.amount_due == Decimal("-252.40")
    assert (bill.remaining, bill.status) == (0, "paid")


def test_credited_current_charges_never_make_a_positive_total_a_negative_bill(
    ledger: Ledger,
) -> None:
    fortis_account(ledger)
    arrangement(ledger, "fortisbc")
    ledger.file(
        FORTIS,
        fixture("fortisbc_bill.txt")
        .replace("Current charges   $185.42", "Current charges   -$5.00")
        .replace("$397.60", "$207.18"),
    )
    ledger.derive()

    bill = by_label(ledger)["bill-2026-02"]

    assert bill.amount_due == 0
    assert bill.status == "paid"


def test_arrears_above_the_total_owe_only_the_total_and_no_negative_bill(
    ledger: Ledger,
) -> None:
    fortis_account(ledger)
    ledger.file(
        FORTIS,
        fortis_with_arrears(fixture("fortisbc_bill.txt")).replace(
            "Arrears   $100.00", "Arrears   $500.00"
        ),
    )
    ledger.derive()

    found = by_label(ledger)

    assert found["arrears-2026-01"].amount_due == Decimal("397.60")
    assert found["bill-2026-02"].amount_due == 0
    assert owed_total(ledger) == Decimal("397.60")


def test_arrears_on_a_bill_in_credit_make_no_row(ledger: Ledger) -> None:
    fortis_account(ledger)
    ledger.file(
        FORTIS,
        fortis_credited(
            ("-$400.00", "-$450.00"),
            "-$437.82",
            "-$252.40",
            "\n   Arrears   $100.00",
        ),
    )
    ledger.derive()

    (bill,) = rows(ledger)

    assert bill.kind == "bill"
    assert bill.amount_due == Decimal("-252.40")


def test_a_negative_arrears_line_is_no_arrears(ledger: Ledger) -> None:
    fortis_account(ledger)
    ledger.file(
        FORTIS,
        fortis_with_arrears(fixture("fortisbc_bill.txt")).replace(
            "Arrears   $100.00", "Arrears   -$5.00"
        ),
    )
    ledger.derive()

    (bill,) = rows(ledger)

    assert bill.kind == "bill"
    assert bill.amount_due == Decimal("397.60")


# --- path (5): manual debts -----------------------------------------------------------


def test_a_manual_debt_with_no_due_date_exists_with_null_dates(ledger: Ledger) -> None:
    ledger.manual(PAYEES_FILE, "sample-payee,Sample Payee,person,,,")
    ledger.manual(OBLIGATIONS_FILE, "sample-payee,manual_debt,loan,,500.00,,,,")
    ledger.derive()

    (row,) = rows(ledger)

    assert (row.kind, row.period_label) == ("manual_debt", "debt-loan")
    assert row.due_date is None
    assert row.issued_on is None
    assert row.amount_due == row.threshold_due == row.remaining == Decimal("500.00")
    assert row.status == "open"
    assert row.primary_source == "manual:obligations.csv:2"


def test_a_manual_debt_keeps_its_dates_and_does_not_chain(ledger: Ledger) -> None:
    ledger.manual(PAYEES_FILE, "sample-payee,Sample Payee,person,,,")
    ledger.manual(
        OBLIGATIONS_FILE,
        "sample-payee,manual_debt,debt-first,,500.00,,2026-06-01,,2026-01-01",
        "sample-payee,manual_debt,second,,20.00,,,,2026-02-01",
    )
    ledger.derive()

    first, second = rows(ledger)

    assert (first.period_label, second.period_label) == ("debt-first", "debt-second")
    assert (first.issued_on, first.due_date) == (D(2026, 1, 1), D(2026, 6, 1))
    assert (first.status, second.status) == ("open", "open")
    assert owed_total(ledger) == Decimal("520.00")


# --- the five paths and no sixth ------------------------------------------------------


def test_one_derive_over_every_source_makes_exactly_the_five_paths(
    ledger: Ledger,
) -> None:
    accounts(
        ledger,
        ("visa", "Vancity", "credit_card", CARD, ""),
        ("mobile", "Virgin Plus", "bill_vendor", "999000123", ""),
        ("hydro", "FortisBC", "bill_vendor", "9990001234", ""),
    )
    ledger.manual(
        PAYEES_FILE,
        "sample-internet,Sample Internet,vendor,,,",
        "sample-payee,Sample Payee,person,,,",
        "cloud,Cloud Vendor,vendor,SAMPLE PHARMACY,,",
    )
    ledger.file(NEWER_VISA_STATEMENT, fixture("vancity_visa_statement_no_minimum.txt"))
    ledger.file(VIRGIN, fixture("virgin_bill.txt"))
    ledger.file(FORTIS, fortis_with_arrears(fixture("fortisbc_bill.txt")))
    ledger.file("_mail/invoice.eml", "From: billing@example.test\n\nSample invoice\n")
    ledger.file("vancity-visa/2024-02-21.csv", fixture("vancity_visa_export.csv"))
    ledger.file("invoices-received/sample/invoice.pdf", fixture("invoice_received.txt"))
    ledger.manual(
        OBLIGATIONS_FILE,
        "sample-internet,bill,,,64.50,,2026-02-20,_mail/invoice.eml,2026-02-01",
        "sample-payee,manual_debt,loan,,500.00,,,,",
    )
    arrangement(ledger, "virgin-plus", "bill-2026-02")
    ledger.derive()

    kinds = sorted((str(row.kind), str(row.period_label)) for row in rows(ledger))

    assert kinds == [
        ("arrangement_instalment", "inst-plan-1"),
        ("arrangement_instalment", "inst-plan-2"),
        ("arrears", "arrears-2026-01"),
        ("bill", "bill-2026-02"),
        ("bill", "bill-2026-02"),
        ("bill", "bill-2026-02"),
        ("card_statement", "card-2026-03-05"),
        ("manual_debt", "debt-loan"),
    ]


def test_only_stage_14_inserts_an_obligation() -> None:
    """Any call of ``insert(obligation)`` in the package, whatever the import."""
    found = []
    for path in sorted(SRC.rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if (
                isinstance(node, ast.Call)
                and getattr(node.func, "name", getattr(node.func, "id", "")) == "insert"
                and any(
                    isinstance(arg, ast.Name) and arg.id == "obligation"
                    for arg in node.args
                )
            ):
                found.append(path.relative_to(SRC).as_posix())

    assert found == ["derive/stage14.py"]
