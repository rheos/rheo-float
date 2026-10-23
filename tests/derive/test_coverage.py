"""``derive.run``: the coverage matrix (stage 6, FR-6, AC-3a, AC-6).

Seams: ``run`` over fixture documents and manual files, read back from ``coverage`` and
``account.first_known_on``. Every value is synthetic. The test clock's local date is
2026-01-15, so an account's months run to January 2026 unless its data runs later.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
from collections.abc import Iterable

from sqlalchemy import func, select

from rheo_finance.clock import FakeClock
from rheo_finance.derive.coverage import STATES
from rheo_finance.manual.accounts import ACCOUNTS_FILE, IDENTIFIERS_FILE
from rheo_finance.manual.document_periods import DOCUMENT_PERIODS_FILE
from rheo_finance.manual.stage2 import account_id
from rheo_finance.parsers.virgin_bill import INSTITUTION as VIRGIN
from rheo_finance.settings import parse_settings
from rheo_finance.storage.schema import account, coverage, document, rejected_period

from .support import (
    CARD_IDENTIFIER,
    CARD_INSTITUTION,
    CARD_STATEMENT,
    CARD_TRANSACTIONS,
    CSV_IDENTIFIER,
    FIXTURES,
    STATEMENT_IDENTIFIER,
    Ledger,
    csv_row,
)

D = dt.date
VIRGIN_BILL = (FIXTURES / "virgin_bill.txt").read_text(encoding="utf-8")
VIRGIN_NUMBER = "999000123"
LAST_MONTH = D(2026, 1, 1)

Cell = tuple[str, str | None]


def account_row(
    slug: str,
    institution: str,
    kind: str,
    *,
    currencies: str = "CAD",
    opened: str = "",
    closed: str = "",
    missing_before: str = "",
    missing_reason: str = "",
) -> str:
    return (
        f"{slug},{slug.title()},{institution},{kind},{currencies},monthly,,{opened},"
        f"{closed},,,,,{missing_before},{missing_reason}"
    )


def register(ledger: Ledger, *accounts: tuple[str, str]) -> None:
    """Accounts as (account row, identifier)."""
    ledger.manual(ACCOUNTS_FILE, *(row for row, _ in accounts))
    ledger.manual(
        IDENTIFIERS_FILE,
        *(f"{row.split(',')[0]},{ident},number,," for row, ident in accounts),
    )


def card_account(**fields: str) -> tuple[str, str]:
    row = account_row("card", CARD_INSTITUTION, "credit_card", **fields)
    return row, CARD_IDENTIFIER


def register_chequing(ledger: Ledger, *others: tuple[str, str]) -> None:
    """The account the chequing CSV fixtures name, plus ``others``."""
    chequing = account_row("chequing", "Vancity", "chequing")
    ledger.manual(ACCOUNTS_FILE, chequing, *(row for row, _ in others))
    ledger.manual(
        IDENTIFIERS_FILE,
        f"chequing,{CSV_IDENTIFIER},number,,",
        f"chequing,{STATEMENT_IDENTIFIER},number,,",
        *(f"{row.split(',')[0]},{ident},number,," for row, ident in others),
    )


def card_statement(
    period: str, opening: str, closing: str, *lines: str, currency: str = "CAD"
) -> str:
    return "\n".join(
        [
            CARD_STATEMENT,
            f"ACCOUNT {CARD_IDENTIFIER}",
            f"CURRENCY {currency}",
            f"PERIOD {period}",
            f"OPENING {opening}",
            f"CLOSING {closing}",
            *(f"LINE {line}" for line in lines),
        ]
    )


def card_export(*lines: str, identifier: str = CARD_IDENTIFIER) -> str:
    return "\n".join(
        [
            CARD_TRANSACTIONS,
            f"ACCOUNT {identifier}",
            *(f"LINE {line}" for line in lines),
        ]
    )


def chequing_csv(*rows: tuple[str, str, str, str, str]) -> str:
    """Rows as (day, description, withdrawal, deposit, balance), oldest first."""
    return "\n".join(
        csv_row(day, description, out=out, into=into, balance=balance)
        for day, description, out, into, balance in rows
    )


def matrix(ledger: Ledger, slug: str) -> dict[dt.date, Cell]:
    rows = ledger.conn.execute(
        select(coverage.c.month, coverage.c.state, coverage.c.reason_code)
        .where(coverage.c.account_id == account_id(slug))
        .order_by(coverage.c.month)
    )
    return {row.month: (row.state, row.reason_code) for row in rows}


def months(first: dt.date, last: dt.date) -> list[dt.date]:
    found: list[dt.date] = []
    current = first
    while current <= last:
        found.append(current)
        current = D(current.year + current.month // 12, current.month % 12 + 1, 1)
    return found


def first_known(ledger: Ledger, slug: str) -> dt.date | None:
    return ledger.conn.scalar(
        select(account.c.first_known_on).where(account.c.slug == slug)
    )


def states(cells: Iterable[Cell]) -> set[str]:
    return {state for state, _ in cells}


# --- exactly one state per account-month ----------------------------------------------


def test_every_account_month_holds_exactly_one_state_from_its_first_known_date(
    ledger: Ledger, card_parsers: None
) -> None:
    register_chequing(
        ledger,
        card_account(),
        (account_row("opened-only", "Sample Bank", "savings", opened="2025-10-20"),
         "999000000041"),
        (account_row("no-data", "Sample Bank", "savings"), "999000000052"),
    )  # fmt: skip
    ledger.file(
        "vancity/export.csv",
        chequing_csv(
            ("03-Mar-2025", "DEPOSIT 99931", "", "100.00", "1100.00"),
            ("09-Jul-2025", "PURCHASE 99932", "25.00", "", "900.00"),
        ),
    )
    ledger.file(
        "synthetic-card/2025-09-30.pdf",
        card_statement("2025-09-01 2025-09-30", "0.00", "-10.00",
                       "2025-09-10 2025-09-09 -10.00 SAMPLE FUEL 99921"),
    )  # fmt: skip

    derived = ledger.derive()

    expected = {
        "chequing": D(2025, 3, 1),
        "card": D(2025, 9, 1),
        "opened-only": D(2025, 10, 1),
    }
    for slug, first in expected.items():
        found = matrix(ledger, slug)
        assert list(found) == months(first, LAST_MONTH), slug
        assert states(found.values()) <= set(STATES)
    total = ledger.conn.scalar(select(func.count()).select_from(coverage))
    assert total == sum(len(months(f, LAST_MONTH)) for f in expected.values())
    assert sum(derived.months.values()) == total
    # No opened date and no data: no months, and no first known date.
    assert matrix(ledger, "no-data") == {}
    assert first_known(ledger, "no-data") is None
    assert first_known(ledger, "chequing") == D(2025, 3, 3)
    assert first_known(ledger, "opened-only") == D(2025, 10, 20)


def test_a_second_derive_gives_the_same_matrix(ledger: Ledger) -> None:
    register_chequing(ledger)
    ledger.file(
        "vancity/export.csv",
        chequing_csv(
            ("03-Mar-2025", "DEPOSIT 99931", "", "100.00", "1100.00"),
            ("09-Jul-2025", "PURCHASE 99932", "25.00", "", "900.00"),
        ),
    )
    ledger.derive()
    first = matrix(ledger, "chequing")
    ledger.derive()
    assert matrix(ledger, "chequing") == first


# --- running balance ------------------------------------------------------------------


def test_a_running_balance_jump_shows_missing_across_the_jump(ledger: Ledger) -> None:
    register_chequing(ledger)
    ledger.file(
        "vancity/export.csv",
        chequing_csv(
            ("01-Jan-2025", "DEPOSIT 99931", "", "100.00", "1100.00"),
            ("15-Jan-2025", "PURCHASE 99932", "50.00", "", "1050.00"),
            ("31-Jan-2025", "PURCHASE 99933", "10.00", "", "1040.00"),
            # Rows are missing here: 1040.00 less 20.00 is not 900.00.
            ("01-Apr-2025", "PURCHASE 99934", "20.00", "", "900.00"),
            ("30-Apr-2025", "DEPOSIT 99935", "", "10.00", "910.00"),
        ),
    )

    ledger.derive()

    found = matrix(ledger, "chequing")
    assert found[D(2025, 1, 1)] == ("have", None)
    assert found[D(2025, 2, 1)] == ("missing", "running_balance_break")
    assert found[D(2025, 3, 1)] == ("missing", "running_balance_break")
    assert found[D(2025, 4, 1)] == ("have", None)
    # After the export's last line nothing is known.
    assert found[D(2025, 5, 1)] == ("missing", None)
    assert first_known(ledger, "chequing") == D(2025, 1, 1)


def test_a_jump_inside_one_month_leaves_it_partial(ledger: Ledger) -> None:
    register_chequing(ledger)
    ledger.file(
        "vancity/export.csv",
        chequing_csv(
            ("01-Jan-2025", "DEPOSIT 99931", "", "100.00", "1100.00"),
            ("10-Jan-2025", "PURCHASE 99932", "50.00", "", "1050.00"),
            ("20-Jan-2025", "PURCHASE 99933", "10.00", "", "999.00"),
            ("31-Jan-2025", "PURCHASE 99934", "9.00", "", "990.00"),
        ),
    )

    ledger.derive()

    assert matrix(ledger, "chequing")[D(2025, 1, 1)] == (
        "partial",
        "running_balance_break",
    )


def test_the_months_a_jump_reaches_into_are_partial_with_its_reason(
    ledger: Ledger,
) -> None:
    register_chequing(ledger)
    ledger.file(
        "vancity/export.csv",
        chequing_csv(
            ("01-Jan-2025", "DEPOSIT 99931", "", "100.00", "1100.00"),
            ("15-Jan-2025", "PURCHASE 99932", "50.00", "", "1050.00"),
            # Rows are missing between 15 January and 10 April.
            ("10-Apr-2025", "PURCHASE 99933", "20.00", "", "900.00"),
            ("30-Apr-2025", "DEPOSIT 99934", "", "10.00", "910.00"),
            ("31-May-2025", "DEPOSIT 99935", "", "10.00", "920.00"),
        ),
    )

    ledger.derive()

    found = matrix(ledger, "chequing")
    assert found[D(2025, 1, 1)] == ("partial", "running_balance_break")
    assert found[D(2025, 2, 1)] == ("missing", "running_balance_break")
    assert found[D(2025, 3, 1)] == ("missing", "running_balance_break")
    assert found[D(2025, 4, 1)] == ("partial", "running_balance_break")
    # The run after the jump covers May whole.
    assert found[D(2025, 5, 1)] == ("have", None)


# --- control none, statements, currencies ---------------------------------------------


def test_a_control_none_export_never_yields_have(
    ledger: Ledger, card_parsers: None
) -> None:
    register(ledger, card_account())
    # A line on every day of February, from an export with no control data.
    ledger.file(
        "synthetic-card/2025-02-28-export.csv",
        card_export(
            "2025-01-30 - -1.00 SAMPLE SHOP 99940",
            *(f"2025-02-{day:02d} - -1.00 SAMPLE SHOP 99940" for day in range(1, 29)),
            "2025-04-02 - -1.00 SAMPLE SHOP 99940",
        ),
    )

    ledger.derive()

    found = matrix(ledger, "card")
    assert found[D(2025, 1, 1)] == ("partial", None)
    assert found[D(2025, 2, 1)] == ("partial", None)
    # No line in March, but inside the export's stated period.
    assert found[D(2025, 3, 1)] == ("partial", "export_period")
    assert found[D(2025, 4, 1)] == ("partial", None)
    assert found[D(2025, 5, 1)] == ("missing", None)
    assert "have" not in states(found.values())


def test_a_tied_out_statement_covering_every_day_gives_have(
    ledger: Ledger, card_parsers: None
) -> None:
    register(ledger, card_account())
    ledger.file(
        "synthetic-card/2025-10-31.pdf",
        card_statement("2025-09-01 2025-10-31", "0.00", "-10.00",
                       "2025-09-10 2025-09-09 -10.00 SAMPLE FUEL 99921"),
    )  # fmt: skip
    ledger.file(
        "synthetic-card/2025-12-15.pdf",
        card_statement("2025-11-01 2025-12-15", "0.00", "0.00"),
    )

    ledger.derive()

    found = matrix(ledger, "card")
    assert found[D(2025, 9, 1)] == ("have", None)
    assert found[D(2025, 10, 1)] == ("have", None), "a month with no line is still had"
    assert found[D(2025, 11, 1)] == ("have", None)
    assert found[D(2025, 12, 1)] == ("partial", None), "half the month is verified"


def test_a_two_currency_account_needs_every_currency_verified(
    ledger: Ledger, card_parsers: None
) -> None:
    register(ledger, card_account(currencies="CAD;USD"))
    ledger.file(
        "synthetic-card/2025-09-30.pdf",
        card_statement("2025-09-01 2025-09-30", "0.00", "0.00"),
    )
    ledger.derive()
    assert matrix(ledger, "card")[D(2025, 9, 1)] == ("partial", None)

    ledger.file(
        "synthetic-card/2025-09-30-usd.pdf",
        card_statement("2025-09-01 2025-09-30", "0.00", "0.00", currency="USD"),
    )
    ledger.derive()
    assert matrix(ledger, "card")[D(2025, 9, 1)] == ("have", None)


def test_a_superseded_copy_adds_nothing_its_twin_covers_the_period(
    ledger: Ledger, card_parsers: None
) -> None:
    register(ledger, card_account())
    text = card_statement("2025-09-01 2025-09-30", "0.00", "0.00")
    ledger.file("synthetic-card/2025-09-30.pdf", text)
    ledger.file("synthetic-card/2025-09-30-copy.pdf", text + "\n\n")

    derived = ledger.derive()

    assert derived.documents["superseded"] == 1
    assert matrix(ledger, "card")[D(2025, 9, 1)] == ("have", None)


def test_a_parsed_bill_covers_its_bill_date_month(ledger: Ledger) -> None:
    register(ledger, (account_row("mobile", VIRGIN, "bill_vendor"), VIRGIN_NUMBER))
    ledger.file("virgin-plus/2026-02-02.pdf", VIRGIN_BILL)

    ledger.derive()

    assert matrix(ledger, "mobile") == {D(2026, 2, 1): ("have", None)}


# --- rejected and unparsed ------------------------------------------------------------


def test_a_rejected_statement_with_a_known_period_shows_rejected(
    ledger: Ledger, card_parsers: None
) -> None:
    register(ledger, card_account())
    # Off by one cent: rejected, no lines.
    ledger.file(
        "synthetic-card/2025-11-30.pdf",
        card_statement("2025-11-01 2025-11-30", "0.00", "-10.00",
                       "2025-11-10 2025-11-09 -9.99 SAMPLE FUEL 99921"),
    )  # fmt: skip
    # An export line in the same month does not make it partial.
    ledger.file(
        "synthetic-card/2025-11-20-export.csv",
        card_export("2025-11-20 - -5.00 SAMPLE SHOP 99940"),
    )
    # A download for an account nobody registered: rejected, on no account.
    ledger.file(
        "synthetic-card/2025-12-31.pdf",
        card_export("2025-12-01 - -5.00 SAMPLE SHOP 99940", identifier="999000000099"),
    )

    derived = ledger.derive()

    assert derived.documents["rejected"] == 2
    assert matrix(ledger, "card") == {
        D(2025, 11, 1): ("rejected", None),
        D(2025, 12, 1): ("missing", None),
        LAST_MONTH: ("missing", None),
    }
    assert ledger.conn.scalar(select(func.count()).select_from(rejected_period)) == 1


def test_a_full_statement_outranks_a_rejected_copy_of_its_month(
    ledger: Ledger, card_parsers: None
) -> None:
    register(ledger, card_account())
    ledger.file(
        "synthetic-card/2025-11-30-bad.pdf",
        card_statement("2025-11-01 2025-11-30", "0.00", "-1.00"),
    )
    ledger.file(
        "synthetic-card/2025-11-30.pdf",
        card_statement("2025-11-01 2025-11-30", "0.00", "0.00"),
    )

    ledger.derive()

    assert matrix(ledger, "card")[D(2025, 11, 1)] == ("have", None)


def test_a_bill_whose_identity_fails_shows_its_month_rejected(ledger: Ledger) -> None:
    register(ledger, (account_row("mobile", VIRGIN, "bill_vendor"), VIRGIN_NUMBER))
    total = "Total amount due   Please pay by* Feb 23, 2026   $126.30"
    ledger.file(
        "virgin-plus/2026-02-02.pdf",
        VIRGIN_BILL.replace(total, total.replace("126.30", "126.31")),
    )

    ledger.derive()

    assert matrix(ledger, "mobile") == {D(2026, 2, 1): ("rejected", None)}


def test_an_unparsed_document_with_an_entered_period_shows_unparsed(
    ledger: Ledger,
) -> None:
    register_chequing(ledger)
    ledger.file("vancity/scan.csv", "not,a,layout\n")
    ledger.file(
        "vancity/export.csv",
        chequing_csv(
            ("03-Oct-2025", "DEPOSIT 99931", "", "100.00", "1100.00"),
            ("09-Oct-2025", "PURCHASE 99932", "25.00", "", "1075.00"),
        ),
    )
    ledger.manual(
        DOCUMENT_PERIODS_FILE,
        "vancity/scan.csv,chequing,2025-06-01,2025-06-30,",
        # The export is parsed: an entered period for it counts for nothing.
        "vancity/export.csv,chequing,2025-08-01,2025-08-31,",
    )

    ledger.derive()

    statuses = set(ledger.conn.scalars(select(document.c.status)))
    assert statuses == {"unparsed", "parsed"}
    found = matrix(ledger, "chequing")
    assert found[D(2025, 6, 1)] == ("unparsed", None)
    assert found[D(2025, 7, 1)] == ("missing", None)
    assert found[D(2025, 8, 1)] == ("missing", None)
    assert first_known(ledger, "chequing") == D(2025, 6, 1)


def test_file_names_are_ignored(ledger: Ledger) -> None:
    register_chequing(ledger)
    # Dated 2019 by name, January 2025 by content.
    ledger.file(
        "vancity/2019-03-31.csv",
        chequing_csv(
            ("01-Jan-2025", "DEPOSIT 99931", "", "100.00", "1100.00"),
            ("31-Jan-2025", "PURCHASE 99932", "10.00", "", "1090.00"),
        ),
    )
    # Unreadable, named for a month, with no entered period: no month at all.
    ledger.file("vancity/2018-05-31.csv", "not,a,layout\n")

    ledger.derive()

    found = matrix(ledger, "chequing")
    assert min(found) == D(2025, 1, 1)
    assert found[D(2025, 1, 1)] == ("have", None)


# --- the account's own dates ----------------------------------------------------------


def test_months_outside_the_open_dates_are_not_applicable(
    ledger: Ledger, card_parsers: None
) -> None:
    register(ledger, card_account(opened="2025-10-01", closed="2025-11-15"))
    # A statement from before the opened date.
    ledger.file(
        "synthetic-card/2025-09-30.pdf",
        card_statement("2025-09-01 2025-09-30", "0.00", "0.00"),
    )

    ledger.derive()

    assert matrix(ledger, "card") == {
        D(2025, 9, 1): ("not_applicable", None),
        D(2025, 10, 1): ("missing", None),
        D(2025, 11, 1): ("missing", None),
        D(2025, 12, 1): ("not_applicable", None),
        LAST_MONTH: ("not_applicable", None),
    }


def test_a_statement_covering_the_open_days_of_the_closing_month_gives_have(
    ledger: Ledger, card_parsers: None
) -> None:
    register(ledger, card_account(opened="2025-10-01", closed="2025-11-15"))
    ledger.file(
        "synthetic-card/2025-11-15.pdf",
        card_statement("2025-10-01 2025-11-15", "0.00", "0.00"),
    )

    ledger.derive()

    found = matrix(ledger, "card")
    assert found[D(2025, 10, 1)] == ("have", None)
    assert found[D(2025, 11, 1)] == ("have", None)


def test_months_before_missing_before_are_missing_with_its_reason(
    ledger: Ledger, card_parsers: None
) -> None:
    register(
        ledger,
        card_account(opened="2025-09-01", missing_before="2025-11-01",
                     missing_reason="mail_purged"),
    )  # fmt: skip
    # Data before the date still counts.
    ledger.file(
        "synthetic-card/2025-09-30.pdf",
        card_statement("2025-09-01 2025-09-30", "0.00", "0.00"),
    )

    ledger.derive()

    assert matrix(ledger, "card") == {
        D(2025, 9, 1): ("have", None),
        D(2025, 10, 1): ("missing", "mail_purged"),
        D(2025, 11, 1): ("missing", None),
        D(2025, 12, 1): ("missing", None),
        LAST_MONTH: ("missing", None),
    }


# --- fixtures from the parser suites --------------------------------------------------


def test_the_jump_fixture_never_shows_its_month_as_have(ledger: Ledger) -> None:
    # Rows are missing between two February lines: the month is partial for the jump,
    # and with no whole month inside it no month is missing.
    register_chequing(ledger)
    ledger.file(
        "vancity/jump.csv",
        (FIXTURES / "vancity_chequing_jump.csv").read_text(encoding="utf-8"),
    )

    ledger.derive()

    assert matrix(ledger, "chequing") == {
        D(2026, 2, 1): ("partial", "running_balance_break")
    }


def test_a_statement_failing_its_checks_shows_rejected_on_every_account_it_names(
    ledger: Ledger,
) -> None:
    # The statement names four accounts; two are registered. Its period runs from 11
    # December to 10 January.
    register(
        ledger,
        (account_row("chequing", "Vancity", "chequing"), "999000000022"),
        (account_row("savings", "Vancity", "savings"), "999000000033"),
    )
    text = (FIXTURES / "vancity_statement_2026.txt").read_text(encoding="utf-8")
    summary = "642.00          12.45"
    assert text.count(summary) == 1
    ledger.file("vancity/2026-01-11.pdf", text.replace(summary, "642.00   12.46"))

    derived = ledger.derive()

    assert derived.documents["rejected"] == 1
    expected = {D(2025, 12, 1): ("rejected", None), LAST_MONTH: ("rejected", None)}
    assert matrix(ledger, "chequing") == expected
    assert matrix(ledger, "savings") == expected
    assert first_known(ledger, "chequing") == D(2025, 12, 11)


def test_an_image_awaiting_manual_entry_shows_unparsed_over_a_line(
    ledger: Ledger,
) -> None:
    register_chequing(ledger)
    ledger.file("vancity/photo.jpg", "synthetic photo bytes")
    ledger.file(
        "vancity/export.csv",
        chequing_csv(("10-Jun-2025", "DEPOSIT 99931", "", "100.00", "1100.00")),
    )
    ledger.manual(
        DOCUMENT_PERIODS_FILE, "vancity/photo.jpg,chequing,2025-06-01,2025-07-15,"
    )

    ledger.derive()

    found = matrix(ledger, "chequing")
    assert found[D(2025, 6, 1)] == ("unparsed", None)
    assert found[D(2025, 7, 1)] == ("unparsed", None)
    assert found[D(2025, 8, 1)] == ("missing", None)


def test_a_trading_export_never_counts(ledger: Ledger) -> None:
    register_chequing(ledger)
    ledger.file("blofin/trades.csv", "time,pair,result\n")
    ledger.manual(
        DOCUMENT_PERIODS_FILE, "blofin/trades.csv,chequing,2025-06-01,2025-06-30,"
    )

    ledger.derive()

    statuses = set(ledger.conn.scalars(select(document.c.status)))
    assert statuses == {"unparsed"}
    assert matrix(ledger, "chequing") == {}
    assert first_known(ledger, "chequing") is None


def test_an_opened_date_starts_the_months_before_the_first_document(
    ledger: Ledger, card_parsers: None
) -> None:
    register(ledger, card_account(opened="2025-08-10"))
    ledger.file(
        "synthetic-card/2025-10-31.pdf",
        card_statement("2025-10-01 2025-10-31", "0.00", "0.00"),
    )

    ledger.derive()

    assert matrix(ledger, "card") == {
        D(2025, 8, 1): ("missing", None),
        D(2025, 9, 1): ("missing", None),
        D(2025, 10, 1): ("have", None),
        D(2025, 11, 1): ("missing", None),
        D(2025, 12, 1): ("missing", None),
        LAST_MONTH: ("missing", None),
    }
    assert first_known(ledger, "card") == D(2025, 8, 10)


def test_missing_before_names_only_months_nothing_else_covers(
    ledger: Ledger, card_parsers: None
) -> None:
    register(
        ledger,
        card_account(opened="2025-08-01", missing_before="2025-11-01",
                     missing_reason="mail_purged"),
    )  # fmt: skip
    # An unverified export line in October, before the date.
    ledger.file(
        "synthetic-card/2025-10-31-export.csv",
        card_export("2025-10-20 - -5.00 SAMPLE SHOP 99940"),
    )

    ledger.derive()

    found = matrix(ledger, "card")
    assert found[D(2025, 8, 1)] == ("missing", "mail_purged")
    assert found[D(2025, 9, 1)] == ("missing", "mail_purged")
    assert found[D(2025, 10, 1)] == ("partial", None)
    assert found[D(2025, 11, 1)] == ("missing", None)


def test_the_months_run_to_the_local_month_not_the_utc_one(ledger: Ledger) -> None:
    register(
        ledger,
        (account_row("opened-only", "Sample Bank", "savings", opened="2025-12-20"),
         "999000000041"),
    )  # fmt: skip
    # 00:30 on 1 February in a zone east of UTC, still 31 January in UTC.
    ledger.ctx = dataclasses.replace(
        ledger.ctx,
        clock=FakeClock(dt.datetime(2026, 1, 31, 15, 30, tzinfo=dt.UTC)),
        settings=parse_settings(
            f'[finance.paths]\narchive = "{ledger.archive}"\n'
            f'downloads = "{ledger.archive.parent / "downloads-unset"}"\n'
            '[finance.locale]\ntimezone = "Asia/Tokyo"\n'
        ),
    )

    ledger.derive()

    assert list(matrix(ledger, "opened-only")) == [
        D(2025, 12, 1), LAST_MONTH, D(2026, 2, 1),
    ]  # fmt: skip
