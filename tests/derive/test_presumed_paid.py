"""Presumed paid, the reminders floor and the presumed-paid candidates (FR-19, FR-26,
FR-67; SPEC Derivation stage 14 "Presumed paid" and "The paying account of a payee";
edge case 80; AC-10's ``paid_from``, AC-82's presumed-paid clause).

Seams: ``derive.presumed.presumed_paid_candidates`` after a derive, and ``derive.run``
as the statuses and the floor marker it leaves on ``obligation``, over synthetic
chequing exports whose running balances make the coverage, typed-in bills and the
owner's decisions recorded through ``finance.decision.record``. Every date, amount and
name is invented.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import uuid
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import Row as SqlRow
from sqlalchemy import select

from rheo_finance.derive.presumed import presumed_paid_candidates
from rheo_finance.derive.run import run
from rheo_finance.derive.snapshot import txn_snapshot
from rheo_finance.manual.payees import PAYEES_FILE
from rheo_finance.storage.schema import obligation, payee

from .support import CSV_IDENTIFIER, SECOND, D, Ledger, read_as_text
from .test_invoice_decisions import reverse
from .test_payment_application import (
    Applied,
    applied,
    bills,
    decide,
    line_ref,
    with_settings,
)
from .test_reversals import export

ISP = "PRE-AUTHORIZED DEBIT SAMPLE ISP 99911"
LANDLORD = "INTERAC E-TRANSFER  To: SAMPLE LANDLORD"
FLOOR = "[finance.reminders]\nmin_due_date = 2023-09-01\n"

Line = tuple[dt.date, str, Decimal]


def statement(ledger: Ledger, name: str, identifier: str, *rows: Line) -> None:
    """One chequing export whose running balance verifies its first to last day."""
    ledger.file(
        f"vancity/{name}.csv",
        export(*((identifier, day, words, amount) for day, words, amount in rows)),
    )


def filler(day: dt.date, n: int) -> Line:
    return (day, f"SAMPLE SHOP 9993{n}", Decimal("-5.00"))


def payees(ledger: Ledger, *, landlord_paid_from: str = "") -> None:
    ledger.manual(
        PAYEES_FILE,
        "isp,Our net,vendor,SAMPLE ISP,,",
        f"landlord,Our rent,person,SAMPLE LANDLORD,,{landlord_paid_from}",
        "net,Our line,vendor,SAMPLE NET,,",
    )


def history(ledger: Ledger, *, landlord_paid_from: str = "") -> None:
    """Two chequing accounts. ``chequing`` is had January to March and July to August
    2023, so April to June and every month from September are gaps; it paid the ISP
    and the landlord in February. ``second`` is had in December 2022 and March to
    August 2023, so January and February are gaps. The floor is 1 September 2023."""
    ledger.accounts(
        ("chequing", "Vancity", "chequing", CSV_IDENTIFIER),
        ("second", "Vancity", "chequing", SECOND),
    )
    payees(ledger, landlord_paid_from=landlord_paid_from)
    with_settings(ledger, FLOOR)
    statement(
        ledger,
        "chequing-q1",
        CSV_IDENTIFIER,
        filler(D(2023, 1, 1), 1),
        (D(2023, 2, 10), ISP, Decimal("-70.00")),
        (D(2023, 2, 15), LANDLORD, Decimal("-500.00")),
        filler(D(2023, 3, 31), 2),
    )
    statement(
        ledger,
        "chequing-summer",
        CSV_IDENTIFIER,
        filler(D(2023, 7, 1), 3),
        filler(D(2023, 8, 31), 4),
    )
    statement(
        ledger,
        "second-dec",
        SECOND,
        filler(D(2022, 12, 1), 5),
        filler(D(2022, 12, 31), 6),
    )
    statement(
        ledger,
        "second-spring",
        SECOND,
        filler(D(2023, 3, 1), 7),
        filler(D(2023, 8, 31), 8),
    )
    bills(
        ledger,
        # Due in January, a month chequing has, as it has the month after.
        ("isp", "55.00", D(2023, 1, 2), D(2023, 1, 20)),
        # Paid by the February line.
        ("isp", "70.00", D(2023, 2, 1), D(2023, 2, 20)),
        # Due in March, had, but April, the month after, is a gap.
        ("isp", "61.00", D(2023, 3, 1), D(2023, 3, 20)),
        ("isp", "62.00", D(2023, 4, 1), D(2023, 4, 20)),
        ("isp", "63.00", D(2023, 5, 1), D(2023, 5, 20)),
        # After the floor, in a gap.
        ("isp", "64.00", D(2023, 10, 1), D(2023, 10, 20)),
        # Never paid: every chequing account is a paying account.
        ("net", "40.00", D(2023, 1, 2), D(2023, 1, 20)),
        ("net", "41.00", D(2023, 7, 1), D(2023, 7, 20)),
        # Paid from chequing in February, a payment no landlord bill fits.
        ("landlord", "520.00", D(2023, 1, 2), D(2023, 1, 20)),
        ("landlord", "510.00", D(2023, 5, 1), D(2023, 5, 20)),
    )


def rows(ledger: Ledger) -> dict[str, SqlRow[Any]]:
    """Every obligation, as ``<payee slug> <period label>``."""
    found = ledger.conn.execute(
        select(obligation, payee.c.slug).join(
            payee, payee.c.id == obligation.c.payee_id
        )
    ).all()
    return {f"{row.slug} {row.period_label}": row for row in found}


def offered(ledger: Ledger) -> dict[str, tuple[str, ...]]:
    """The candidates, each with the paying accounts that left a gap."""
    label = {row.id: name for name, row in rows(ledger).items()}
    return {
        label[each.obligation_id]: each.gaps
        for each in presumed_paid_candidates(ledger.uow)
    }


def ref(ledger: Ledger, name: str) -> str:
    return f"finance.obligation:{rows(ledger)[name].id}"


def presume(ledger: Ledger, name: str) -> uuid.UUID:
    return decide(ledger, "presumed_paid", obligation_ref=ref(ledger, name))


# --- the candidates -------------------------------------------------------------------


def test_old_bills_whose_paying_account_has_a_gap_are_offered(ledger: Ledger) -> None:
    """Three ISP bills with a gap on chequing, the account that paid the ISP, for the
    due month or the month after; the January bill, whose months chequing has, the
    paid February bill and the bill after the floor are not. The never-paid line's
    January bill has a gap on ``second`` only, its July bill on neither. The landlord,
    paid from chequing, is tested against chequing."""
    history(ledger)

    ledger.derive()

    assert offered(ledger) == {
        "isp bill-2023-03": ("chequing",),
        "isp bill-2023-04": ("chequing",),
        "isp bill-2023-05": ("chequing",),
        "net bill-2023-01": ("second",),
        "landlord bill-2023-05": ("chequing",),
    }


def test_a_paid_from_row_narrows_the_paying_accounts_to_its_own(
    ledger: Ledger,
) -> None:
    """Payment evidence names chequing, ``paid_from`` names ``second``: only
    ``second`` is read, so its January gap offers the January bill and chequing's May
    gap no longer offers the May one."""
    history(ledger, landlord_paid_from="second")

    ledger.derive()

    found = offered(ledger)
    assert found["landlord bill-2023-01"] == ("second",)
    assert "landlord bill-2023-05" not in found


def test_nothing_is_offered_while_the_floor_is_unset(ledger: Ledger) -> None:
    history(ledger)
    with_settings(ledger, "")

    ledger.derive()

    assert offered(ledger) == {}
    assert not any(row.before_floor for row in rows(ledger).values())


# --- the floor ------------------------------------------------------------------------


def test_every_obligation_due_before_the_floor_is_marked_whatever_its_status(
    ledger: Ledger,
) -> None:
    history(ledger)
    ledger.derive()
    presume(ledger, "isp bill-2023-04")

    ledger.derive()

    found = rows(ledger)
    marked = {name for name, row in found.items() if row.before_floor}
    assert marked == set(found) - {"isp bill-2023-10"}
    assert found["isp bill-2023-02"].status == "paid"
    assert found["isp bill-2023-04"].status == "presumed_paid"


def test_the_floor_day_itself_is_not_before_the_floor(ledger: Ledger) -> None:
    history(ledger)
    with_settings(ledger, "[finance.reminders]\nmin_due_date = 2023-10-20\n")

    ledger.derive()

    found = rows(ledger)
    assert not found["isp bill-2023-10"].before_floor
    assert found["isp bill-2023-05"].before_floor


# --- presumed paid --------------------------------------------------------------------


def test_a_presumed_paid_decision_closes_the_bill_and_its_reversal_reopens_it(
    ledger: Ledger,
) -> None:
    history(ledger)
    ledger.derive()
    decided = presume(ledger, "isp bill-2023-04")

    ledger.derive()

    found = rows(ledger)["isp bill-2023-04"]
    assert (found.status, found.presumed_paid_decision_id) == ("presumed_paid", decided)
    assert (found.paid, found.remaining) == (0, Decimal("62.00"))
    assert "isp bill-2023-04" not in offered(ledger)
    assert len(offered(ledger)) == 4
    # Keyed by the natural key, the decision holds across a rebuild.
    run(ledger.uow, ledger.ctx, extract_pdf=read_as_text, reparse=True)
    assert rows(ledger)["isp bill-2023-04"].status == "presumed_paid"

    reverse(ledger, decided)
    ledger.derive()

    found = rows(ledger)["isp bill-2023-04"]
    assert (found.status, found.presumed_paid_decision_id) == ("open", None)
    assert "isp bill-2023-04" in offered(ledger)


def test_a_presumed_paid_decision_leaves_a_bill_with_nothing_owed_paid(
    ledger: Ledger,
) -> None:
    """Only an open obligation becomes presumed paid; the decision still names it."""
    one_bill(ledger)
    bills(ledger, ONE_BILL, ("isp", "0.00", D(2023, 6, 1), D(2023, 6, 20)))
    ledger.derive()
    decided = presume(ledger, "isp bill-2023-06")

    ledger.derive()

    found = rows(ledger)["isp bill-2023-06"]
    assert (found.status, found.presumed_paid_decision_id) == ("paid", decided)


ONE_BILL = ("isp", "62.00", D(2023, 4, 1), D(2023, 4, 20))


def one_bill(ledger: Ledger) -> None:
    """One ISP bill before the floor, presumed paid, and chequing, had in March."""
    ledger.chequing()
    payees(ledger)
    with_settings(ledger, FLOOR)
    # A decision on a line reads the line it names.
    ledger.ctx = dataclasses.replace(ledger.ctx, snapshot_provider=txn_snapshot)
    statement(
        ledger,
        "march",
        CSV_IDENTIFIER,
        filler(D(2023, 3, 1), 1),
        filler(D(2023, 3, 31), 2),
    )
    bills(ledger, ONE_BILL)
    ledger.derive()


def test_a_presumed_paid_bill_reverts_to_paid_when_its_line_loads(
    ledger: Ledger,
) -> None:
    one_bill(ledger)
    decided = presume(ledger, "isp bill-2023-04")
    ledger.derive()
    assert rows(ledger)["isp bill-2023-04"].status == "presumed_paid"

    statement(
        ledger,
        "april",
        CSV_IDENTIFIER,
        filler(D(2023, 4, 1), 3),
        (D(2023, 4, 18), ISP, Decimal("-62.00")),
        filler(D(2023, 4, 30), 4),
    )
    ledger.derive()

    found = rows(ledger)["isp bill-2023-04"]
    assert (found.status, found.paid, found.remaining) == ("paid", Decimal("62.00"), 0)
    assert applied(ledger) == [Applied("bill-2023-04", Decimal("62.00"), True)]
    # The decision stays on record and changes nothing.
    assert found.presumed_paid_decision_id == decided


def test_a_partial_payment_on_a_presumed_paid_bill_shows_what_is_left(
    ledger: Ledger,
) -> None:
    """A 30.00 line does not fit the 62.00 bill, so alone it is a question and the bill
    stays presumed paid; placed on it by the owner, the bill is partial with 32.00
    left."""
    one_bill(ledger)
    presume(ledger, "isp bill-2023-04")
    statement(
        ledger,
        "april",
        CSV_IDENTIFIER,
        filler(D(2023, 4, 1), 3),
        (D(2023, 4, 18), ISP, Decimal("-30.00")),
        filler(D(2023, 4, 30), 4),
    )
    ledger.derive()
    assert rows(ledger)["isp bill-2023-04"].status == "presumed_paid"
    assert applied(ledger) == [Applied("confirm", Decimal("30.00"), True)]

    decide(
        ledger,
        "payment_apply",
        target_ref=line_ref(ledger, D(2023, 4, 18)),
        obligation_ref=ref(ledger, "isp bill-2023-04"),
    )
    ledger.derive()

    found = rows(ledger)["isp bill-2023-04"]
    assert (found.status, found.paid, found.remaining) == (
        "partial",
        Decimal("30.00"),
        Decimal("32.00"),
    )


@pytest.mark.parametrize("presumed", [False, True], ids=["open", "presumed paid"])
def test_a_presumed_paid_bill_is_not_a_live_candidate_for_a_payment(
    ledger: Ledger, presumed: bool
) -> None:
    """Two bills of 62.00 and a payment of 62.00 that could pay either: open, the older
    is paid; presumed paid, the older is not live, so the newer is."""
    one_bill(ledger)
    bills(ledger, ONE_BILL, ("isp", "62.00", D(2023, 4, 5), D(2023, 5, 5)))
    ledger.derive()
    if presumed:
        presume(ledger, "isp bill-2023-04")
    statement(
        ledger,
        "april",
        CSV_IDENTIFIER,
        filler(D(2023, 4, 1), 3),
        (D(2023, 4, 22), ISP, Decimal("-62.00")),
        filler(D(2023, 4, 30), 4),
    )

    ledger.derive()

    statuses = {name: row.status for name, row in rows(ledger).items()}
    assert statuses == {
        "isp bill-2023-04": "presumed_paid" if presumed else "paid",
        "isp bill-2023-05": "paid" if presumed else "open",
    }
