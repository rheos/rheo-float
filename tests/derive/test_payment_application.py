"""Stage 15's application: what each payment paid (FR-21, FR-22; Key decisions 18 and
20; edge cases 14 to 17, 34 and 66; AC-25, AC-10's e-Transfer bill, AC-58's obligation
part).

Seam: ``derive.run`` through the derive ``ledger`` over synthetic chequing exports,
card statements in the Vancity Visa layout with invented figures, typed-in bills and
arrangements, with the owner's decisions recorded through ``finance.decision.record``,
read back as ``obligation``, ``payment_application`` and ``event``. Every date, amount
and name is invented.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import uuid
from decimal import Decimal
from typing import Any, NamedTuple

import pytest
from sqlalchemy import Row, select

from rheo_finance.derive.run import run
from rheo_finance.derive.snapshot import txn_snapshot
from rheo_finance.manual.accounts import ACCOUNTS_FILE
from rheo_finance.manual.arrangements import ARRANGEMENTS_FILE
from rheo_finance.manual.obligations import OBLIGATIONS_FILE
from rheo_finance.manual.payees import PAYEES_FILE
from rheo_finance.settings import parse_settings
from rheo_finance.storage.schema import (
    event,
    obligation,
    payee,
    payment,
    payment_application,
)

from .support import CSV_IDENTIFIER, RULE_SETTINGS, D, Ledger, read_as_text
from .test_invoice_decisions import call, reverse
from .test_obligations import accounts, fortis_bill, fortis_with_arrears
from .test_payments import bank, bank_line, notice, notice_key, set_cell
from .test_transfer_edges import PAYMENT_RECEIVED, file_bank, on, visa_export

ISP = "PRE-AUTHORIZED DEBIT SAMPLE ISP 99911"
LANDLORD = "INTERAC E-TRANSFER  To: SAMPLE LANDLORD"
CANCEL = "E-TRANSFER DEBIT REVERSAL CUSTOMER CANCEL"
NET = "INTERAC E-TRANSFER  To: SAMPLE NET"
MOBILE = "PRE-AUTHORIZED DEBIT SAMPLE MOBILE 99912"
POWER = "PRE-AUTHORIZED DEBIT SAMPLE POWER 99913"
SHOP = "SAMPLE SHOP SAMPLE CITY"
JUNE = D(2025, 6, 10)


class Applied(NamedTuple):
    """One application as a test reads it: the obligation's period, or the credit's
    reason, and the amount."""

    to: str
    amount: Decimal
    auto: bool


def applied(ledger: Ledger) -> list[Applied]:
    rows = ledger.conn.execute(
        select(
            obligation.c.period_label,
            payment_application.c.reason,
            payment_application.c.amount,
            payment_application.c.auto,
        ).select_from(
            payment_application.outerjoin(
                obligation, obligation.c.id == payment_application.c.obligation_id
            )
        )
    ).all()
    return sorted(
        Applied(row.period_label or row.reason, row.amount, row.auto) for row in rows
    )


def owed(ledger: Ledger) -> dict[str, Row[Any]]:
    return {
        row.period_label: row for row in ledger.conn.execute(select(obligation)).all()
    }


def figures(ledger: Ledger, label: str) -> tuple[object, ...]:
    """An obligation's paid, remaining, threshold remaining and status."""
    row = owed(ledger)[label]
    return (row.paid, row.remaining, row.threshold_remaining, row.status)


def bills(ledger: Ledger, *rows: tuple[str, str, dt.date, dt.date]) -> None:
    """Typed-in bills as ``(payee, amount due, issued on, due on)``."""
    ledger.manual(
        OBLIGATIONS_FILE,
        *(
            f"{who},bill,,,{amount},,{due},,{issued}"
            for who, amount, issued, due in rows
        ),
    )


def with_settings(ledger: Ledger, text: str) -> None:
    ledger.ctx = dataclasses.replace(
        ledger.ctx,
        settings=parse_settings(
            f'[finance.paths]\narchive = "{ledger.archive}"\n'
            f'downloads = "{ledger.archive.parent / "downloads-unset"}"\n'
            + RULE_SETTINGS
            + text
        ),
    )


def line_ref(ledger: Ledger, day: dt.date) -> str:
    return f"finance.txn:{bank_line(ledger, day)}"


def obligation_ref(ledger: Ledger, label: str) -> str:
    return f"finance.obligation:{owed(ledger)[label].id}"


def decide(ledger: Ledger, kind: str, **fields: object) -> uuid.UUID:
    out = call(ledger, "finance.decision.record", {"kind": kind, **fields})
    decision_id = getattr(out, "decision_id", None)
    assert isinstance(decision_id, uuid.UUID)
    return decision_id


@pytest.fixture
def paying(own: Ledger, notice_parser: None) -> Ledger:
    """The ``own`` accounts with the Visa named by its payment wording, a bill vendor's
    account whose bills chain, and vendors and a landlord whose bills do not."""
    with (own.data / "manual" / ACCOUNTS_FILE).open("a", encoding="utf-8") as out:
        out.write(
            "mobile,Mobile,Sample Mobile,bill_vendor,CAD,monthly,,,,,,"
            "SAMPLE MOBILE,,,\n"
        )
    own.add_identifier("mobile", "999000123")
    set_cell(own, "visa", "payee_patterns", "VANCITY VISA")
    own.manual(
        PAYEES_FILE,
        "isp,Our net,vendor,SAMPLE ISP,,",
        "landlord,Our rent,person,SAMPLE LANDLORD,,",
        "net,Our line,vendor,SAMPLE NET,,",
    )
    own.ctx = dataclasses.replace(own.ctx, snapshot_provider=txn_snapshot)
    return own


# --- bills and every other kind (AC-25) -----------------------------------------------


@pytest.mark.parametrize("amount", ["200.00", "199.99", "200.01"])
def test_one_open_obligation_and_an_equal_payment_applies(
    paying: Ledger, amount: str
) -> None:
    bills(paying, ("isp", "200.00", D(2025, 6, 1), D(2025, 6, 20)))
    bank(paying, (JUNE, ISP, -Decimal(amount)))

    paying.derive()

    assert applied(paying) == [Applied("bill-2025-06", Decimal(amount), True)]
    assert figures(paying, "bill-2025-06") == (Decimal(amount), 0, 0, "paid")


def test_two_open_obligations_and_a_payment_equal_to_one_applies_to_that_one(
    paying: Ledger,
) -> None:
    bills(
        paying,
        ("isp", "200.00", D(2025, 6, 1), D(2025, 6, 20)),
        ("isp", "150.00", D(2025, 7, 1), D(2025, 7, 20)),
    )
    bank(paying, (D(2025, 7, 10), ISP, Decimal("-150.00")))

    paying.derive()

    assert applied(paying) == [Applied("bill-2025-07", Decimal("150.00"), True)]
    assert figures(paying, "bill-2025-06")[3] == "open"
    assert figures(paying, "bill-2025-07")[3] == "paid"


def test_two_open_obligations_and_a_payment_matching_neither_applies_nothing(
    paying: Ledger,
) -> None:
    """A confirm item: the credit waits for the owner's answer."""
    bills(
        paying,
        ("isp", "200.00", D(2025, 6, 1), D(2025, 6, 20)),
        ("isp", "150.00", D(2025, 7, 1), D(2025, 7, 20)),
    )
    bank(paying, (D(2025, 7, 10), ISP, Decimal("-175.00")))

    paying.derive()

    assert applied(paying) == [Applied("confirm", Decimal("175.00"), True)]
    assert {row.paid for row in owed(paying).values()} == {0}


def test_a_partial_payment_toward_an_arrangement_applies_oldest_first(
    paying: Ledger,
) -> None:
    paying.manual(
        ARRANGEMENTS_FILE,
        "plan,isp,2025-05-01,1,50.00,2025-06-01,",
        "plan,isp,2025-05-01,2,60.00,2025-07-01,",
    )
    bank(paying, (JUNE, ISP, Decimal("-80.00")))

    paying.derive()

    assert applied(paying) == [
        Applied("inst-plan-1", Decimal("50.00"), True),
        Applied("inst-plan-2", Decimal("30.00"), True),
    ]
    assert figures(paying, "inst-plan-1") == (Decimal("50.00"), 0, 0, "paid")
    assert figures(paying, "inst-plan-2") == (
        Decimal("30.00"),
        Decimal("30.00"),
        Decimal("30.00"),
        "partial",
    )


def test_an_instalment_overpaid_leaves_the_rest_as_excess(paying: Ledger) -> None:
    paying.manual(ARRANGEMENTS_FILE, "plan,isp,2025-05-01,1,50.00,2025-06-01,")
    bank(paying, (JUNE, ISP, Decimal("-80.00")))

    paying.derive()

    assert applied(paying) == [
        Applied("excess", Decimal("30.00"), True),
        Applied("inst-plan-1", Decimal("50.00"), True),
    ]


@pytest.mark.parametrize(
    ("before", "applies"),
    [(3, True), (10, True), (11, False), (20, False)],
    ids=["3 days", "10 days", "11 days", "20 days"],
)
def test_a_payment_before_its_bill_applies_when_the_bill_loads_within_early_days(
    paying: Ledger, before: int, applies: bool
) -> None:
    issued = D(2025, 6, 13)
    paid_on = issued - dt.timedelta(days=before)
    bank(paying, (paid_on, ISP, Decimal("-200.00")))
    paying.derive()
    (credit,) = paying.conn.execute(select(payment_application)).all()
    assert (credit.reason, credit.obligation_id) == ("no_target", None)
    assert credit.bound_on == paid_on + dt.timedelta(days=10)

    bills(paying, ("isp", "200.00", issued, D(2025, 7, 2)))
    paying.derive()

    if applies:
        assert applied(paying) == [Applied("bill-2025-07", Decimal("200.00"), True)]
        assert figures(paying, "bill-2025-07")[3] == "paid"
    else:
        assert applied(paying) == [Applied("no_target", Decimal("200.00"), True)]
        assert figures(paying, "bill-2025-07") == (
            0,
            Decimal("200.00"),
            Decimal("200.00"),
            "open",
        )


def test_a_payment_years_before_the_only_open_bill_stays_a_credit(
    paying: Ledger,
) -> None:
    """It never rolls forward, however many derives or later bills follow."""
    bank(paying, (D(2022, 6, 10), ISP, Decimal("-200.00")))

    for newer in ((), (("isp", "200.00", D(2025, 7, 1), D(2025, 7, 20)),)):
        bills(paying, ("isp", "200.00", D(2025, 6, 1), D(2025, 6, 20)), *newer)
        paying.derive()
        paying.derive()

        assert applied(paying) == [Applied("no_target", Decimal("200.00"), True)]
        assert all(row.remaining == Decimal("200.00") for row in owed(paying).values())
    bound = paying.conn.scalar(select(payment_application.c.bound_on))
    assert bound == D(2022, 6, 20)


@pytest.mark.parametrize(
    ("day", "amount", "to"),
    [
        (D(2020, 3, 15), "80.00", "bill-2020-03"),
        (D(2020, 2, 20), "80.00", "bill-2020-03"),
        (D(2023, 4, 15), "80.00", "bill-2020-03"),
        (D(2023, 4, 25), "95.00", "bill-2023-05"),
        (D(2020, 3, 15), "70.00", "confirm"),
        (D(2020, 2, 19), "80.00", "no_target"),
    ],
    ids=[
        "inside",
        "early days before",
        "before the current bill is a candidate",
        "the current bill",
        "does not fit",
        "before the window",
    ],
)
def test_a_payment_inside_a_superseded_bills_window_lands_on_that_bill(
    paying: Ledger, day: dt.date, amount: str, to: str
) -> None:
    """A bill vendor's bills chain, so the old one is superseded and the current one
    owes what is left. A 2020 payment pays the 2020 bill or nothing."""
    bills(
        paying,
        ("mobile", "80.00", D(2020, 3, 1), D(2020, 3, 20)),
        ("mobile", "95.00", D(2023, 5, 1), D(2023, 5, 20)),
    )
    bank(paying, (day, MOBILE, -Decimal(amount)))

    paying.derive()

    assert applied(paying) == [Applied(to, Decimal(amount), True)]
    old, current = owed(paying)["bill-2020-03"], owed(paying)["bill-2023-05"]
    assert old.status == "superseded"
    assert old.paid == (Decimal(amount) if to == "bill-2020-03" else 0)
    if to != "bill-2023-05":
        assert (current.remaining, current.status) == (Decimal("95.00"), "open")


@pytest.mark.parametrize(
    ("day", "to"),
    [(D(2023, 5, 10), "bill-2020-03"), (D(2023, 5, 11), "excess")],
    ids=["inside", "at the end"],
)
def test_an_old_bills_window_ends_at_the_next_bill_plus_early_days(
    paying: Ledger, day: dt.date, to: str
) -> None:
    """With the current bill paid there is no live candidate, so the window decides;
    past it, the payment found only bills already paid and is an overpayment."""
    bills(
        paying,
        ("mobile", "80.00", D(2020, 3, 1), D(2020, 3, 20)),
        ("mobile", "95.00", D(2023, 5, 1), D(2023, 5, 20)),
    )
    bank(
        paying,
        (D(2023, 5, 2), MOBILE, Decimal("-95.00")),
        (day, MOBILE, Decimal("-80.00")),
    )

    paying.derive()

    assert applied(paying) == sorted(
        [
            Applied("bill-2023-05", Decimal("95.00"), True),
            Applied(to, Decimal("80.00"), True),
        ]
    )


def test_a_payment_in_another_currency_pays_nothing(paying: Ledger) -> None:
    bills(paying, ("isp", "200.00", D(2025, 6, 1), D(2025, 6, 20)))
    notice(paying, JUNE, currency="USD")

    paying.derive()

    assert applied(paying) == [Applied("no_target", Decimal("200.00"), True)]
    assert figures(paying, "bill-2025-06")[3] == "open"


# --- reversed payments (edge cases 17 and 34; AC-58) ----------------------------------


def test_a_reversed_payment_never_reduces_the_bill_and_reopens_one_it_closed(
    paying: Ledger,
) -> None:
    bills(paying, ("landlord", "200.00", D(2025, 6, 1), D(2025, 6, 20)))
    bank(paying, (JUNE, LANDLORD, Decimal("-200.00")))
    paying.derive()
    assert figures(paying, "bill-2025-06")[3] == "paid"

    bank(
        paying,
        (JUNE, LANDLORD, Decimal("-200.00")),
        (D(2025, 6, 12), CANCEL, Decimal("200.00")),
    )
    paying.derive()

    assert paying.conn.scalar(select(payment.c.state)) == "reversed"
    assert applied(paying) == []
    assert figures(paying, "bill-2025-06") == (
        0,
        Decimal("200.00"),
        Decimal("200.00"),
        "open",
    )


def test_a_cancelled_e_transfer_and_its_resend_pay_the_rent_once(
    paying: Ledger,
) -> None:
    """The shape of AC-58 on invented dates and amounts: a debit, its cancellation a
    few days on, and the same payment sent again that day."""
    bills(paying, ("landlord", "850.00", D(2025, 9, 1), D(2025, 9, 5)))
    bank(
        paying,
        (D(2025, 9, 3), LANDLORD, Decimal("-850.00")),
        (D(2025, 9, 9), CANCEL, Decimal("850.00")),
        (D(2025, 9, 9), LANDLORD, Decimal("-850.00")),
    )

    paying.derive()

    assert applied(paying) == [Applied("bill-2025-09", Decimal("850.00"), True)]
    assert figures(paying, "bill-2025-09")[3] == "paid"
    applied_from = paying.conn.scalar(
        select(payment.c.paid_on).join(
            payment_application, payment_application.c.payment_id == payment.c.id
        )
    )
    assert applied_from == D(2025, 9, 9)


def test_a_typed_in_bill_is_closed_by_a_later_e_transfer_of_its_amount(
    paying: Ledger,
) -> None:
    bills(paying, ("net", "64.50", D(2025, 6, 1), D(2025, 6, 20)))
    bank(paying, (D(2025, 6, 15), NET, Decimal("-64.50")))

    paying.derive()

    assert figures(paying, "bill-2025-06") == (Decimal("64.50"), 0, 0, "paid")


# --- the owner's decisions ------------------------------------------------------------


def test_undo_restores_the_remaining_and_reversing_the_undo_applies_again(
    paying: Ledger,
) -> None:
    bills(paying, ("isp", "200.00", D(2025, 6, 1), D(2025, 6, 20)))
    bank(paying, (JUNE, ISP, Decimal("-200.00")))
    paying.derive()
    before = figures(paying, "bill-2025-06")

    undo = decide(
        paying,
        "payment_unapply",
        target_ref=line_ref(paying, JUNE),
        obligation_ref=obligation_ref(paying, "bill-2025-06"),
    )
    paying.derive()

    assert figures(paying, "bill-2025-06") == (
        0,
        Decimal("200.00"),
        Decimal("200.00"),
        "open",
    )
    assert applied(paying) == [Applied("payment_unapply", Decimal("200.00"), False)]
    basis = paying.conn.scalar(select(payment_application.c.basis_ref))
    assert basis == f"finance.decision:{undo}"

    reverse(paying, undo)
    paying.derive()
    assert figures(paying, "bill-2025-06") == before


@pytest.mark.parametrize(
    ("amount", "rows"),
    [
        (None, [Applied("bill-2025-06", Decimal("175.00"), False)]),
        (
            "100.00",
            [
                Applied("bill-2025-06", Decimal("100.00"), False),
                Applied("excess", Decimal("75.00"), False),
            ],
        ),
    ],
    ids=["whole", "an amount"],
)
def test_payment_apply_places_a_payment_the_rules_left_to_the_owner(
    paying: Ledger, amount: str | None, rows: list[Applied]
) -> None:
    bills(
        paying,
        ("isp", "200.00", D(2025, 6, 1), D(2025, 6, 20)),
        ("isp", "150.00", D(2025, 7, 1), D(2025, 7, 20)),
    )
    bank(paying, (D(2025, 7, 10), ISP, Decimal("-175.00")))
    paying.derive()
    assert [each.to for each in applied(paying)] == ["confirm"]

    fields = {} if amount is None else {"amount": amount}
    decide(
        paying,
        "payment_apply",
        target_ref=line_ref(paying, D(2025, 7, 10)),
        obligation_ref=obligation_ref(paying, "bill-2025-06"),
        **fields,
    )
    paying.derive()

    assert applied(paying) == rows
    assert figures(paying, "bill-2025-06")[3] == "partial"
    assert figures(paying, "bill-2025-07")[3] == "open"


def test_a_notice_names_its_payment_in_a_decision(paying: Ledger) -> None:
    bills(paying, ("isp", "200.00", D(2025, 6, 1), D(2025, 6, 20)))
    filed = notice(paying, JUNE, printed="Sample ISP")
    paying.derive()
    assert figures(paying, "bill-2025-06")[3] == "paid"

    decide(
        paying,
        "payment_unapply",
        target_ref=f"finance.payment_notice:{notice_key(paying, filed)}",
    )
    paying.derive()

    assert figures(paying, "bill-2025-06")[3] == "open"
    assert applied(paying) == [Applied("payment_unapply", Decimal("200.00"), False)]


# --- events ---------------------------------------------------------------------------


def test_an_automatic_application_registers_payment_detected_without_an_amount(
    paying: Ledger,
) -> None:
    bills(
        paying,
        ("isp", "200.00", D(2025, 6, 1), D(2025, 6, 20)),
        ("isp", "150.00", D(2025, 7, 1), D(2025, 7, 20)),
    )
    bank(
        paying,
        (JUNE, ISP, Decimal("-200.00")),
        (D(2025, 7, 10), ISP, Decimal("-175.00")),
    )
    paying.derive()

    (row,) = paying.conn.execute(
        select(event).where(event.c.type == "finance.payment.detected")
    ).all()
    isp = paying.conn.scalar(select(payee.c.id).where(payee.c.slug == "isp"))
    assert row.data == {
        "payee_ref": f"finance.payee:{isp}",
        "obligation_ref": obligation_ref(paying, "bill-2025-06"),
    }
    assert row.occurred_on == JUNE

    decide(
        paying,
        "payment_apply",
        target_ref=line_ref(paying, D(2025, 7, 10)),
        obligation_ref=obligation_ref(paying, "bill-2025-07"),
    )
    paying.derive()

    # A decided application is the owner's own: no event for it.
    found = paying.conn.execute(
        select(event.c.id).where(event.c.type == "finance.payment.detected")
    ).all()
    assert len(found) == 1


def test_a_derive_and_a_rebuild_apply_the_same(paying: Ledger) -> None:
    bills(
        paying,
        ("isp", "200.00", D(2025, 6, 1), D(2025, 6, 20)),
        ("mobile", "80.00", D(2020, 3, 1), D(2020, 3, 20)),
        ("mobile", "95.00", D(2023, 5, 1), D(2023, 5, 20)),
    )
    bank(
        paying,
        (JUNE, ISP, Decimal("-200.00")),
        (D(2020, 3, 15), MOBILE, Decimal("-80.00")),
        (D(2022, 1, 5), ISP, Decimal("-20.00")),
    )
    paying.derive()

    def state() -> tuple[list[str], list[str]]:
        return (
            sorted(
                repr(tuple(row))
                for row in paying.conn.execute(select(payment_application))
            ),
            sorted(repr(tuple(row)) for row in paying.conn.execute(select(obligation))),
        )

    derived = state()
    paying.derive()
    assert state() == derived
    run(paying.uow, paying.ctx, extract_pdf=read_as_text, reparse=True)
    assert state() == derived


# --- cards, with no early_days slack --------------------------------------------------

STATEMENT_N = D(2025, 3, 1)
STATEMENT_N1 = D(2025, 3, 31)
CardRow = tuple[dt.date, str, Decimal]


def visa_statement(
    start: dt.date,
    end: dt.date,
    previous: Decimal,
    rows: tuple[CardRow, ...],
    minimum: Decimal,
    due: dt.date,
) -> str:
    """A statement in the Vancity Visa layout with these figures; the summary ties out
    to the lines, which are signed from the owner's side."""
    purchases = sum((-amount for _, _, amount in rows if amount < 0), Decimal(0))
    credits = sum((amount for _, _, amount in rows if amount > 0), Decimal(0))
    balance = previous + purchases - credits

    def cad(value: Decimal) -> str:
        return f"${value:,.2f}"

    def day(value: dt.date) -> str:
        return f"{value:%b}".upper() + f" {value.day:02d}"

    return "\n".join(
        [
            f"Statement period: {start:%B} {start.day} to {end:%B} {end.day},"
            f" {end.year}   Account Number: 9999 99** **** 9991",
            "",
            "Statement summary   Payment information",
            f" Statement balance   {cad(balance)}",
            f" Minimum payment   {cad(minimum)}",
            f" Payment due date   {due:%B} {due.day}, {due.year}",
            f" Previous balance   {cad(previous)}",
            f" Payments & credits   -   {cad(credits)}",
            f" Purchases & debits   +   {cad(purchases)}",
            " Cash advances   +   $0.00",
            " Interest   +   $0.00",
            " Fees   +   $0.00",
            f"Statement balance   {cad(balance)}",
            "",
            *(
                f"{day(when)}   {day(when)} {words}   "
                f"{'- ' if amount > 0 else ''}{cad(abs(amount))}"
                for when, words, amount in rows
            ),
            "",
        ]
    )


def statement_n(ledger: Ledger, *payments: CardRow) -> None:
    """Statement N, dated 1 March: 800.00 owed, a 25.00 minimum due 25 March."""
    ledger.file(
        "vancity-visa/2025-03-01.pdf",
        visa_statement(
            D(2025, 2, 2),
            STATEMENT_N,
            Decimal(0),
            ((D(2025, 2, 10), SHOP, Decimal("-800.00")), *payments),
            Decimal("25.00"),
            D(2025, 3, 25),
        ),
    )


def statement_n1(ledger: Ledger, *payments: CardRow) -> None:
    """Statement N+1, dated 31 March, carrying N's balance and the payments made."""
    ledger.file(
        "vancity-visa/2025-03-31.pdf",
        visa_statement(
            D(2025, 3, 2),
            STATEMENT_N1,
            Decimal("800.00"),
            ((D(2025, 3, 12), SHOP, Decimal("-100.00")), *payments),
            Decimal("30.00"),
            D(2025, 4, 25),
        ),
    )


def card_export(ledger: Ledger, *rows: CardRow) -> None:
    """The card's transactions as downloaded before the next statement."""
    ledger.file("vancity-visa/2025-03-28.csv", visa_export(*rows))


def paid_card(day: dt.date, amount: str) -> CardRow:
    return (day, PAYMENT_RECEIVED, Decimal(amount))


N, N1 = "card-2025-03-01", "card-2025-03-31"


def test_a_minimum_paid_on_its_due_date_stays_on_its_statement_when_the_next_loads(
    paying: Ledger,
) -> None:
    """Edge case 66 and AC-25's card case: the payment is dated before N+1, so N+1's
    balance already nets it, and N+1's minimum stays wholly unpaid."""
    minimum = paid_card(D(2025, 3, 25), "25.00")
    statement_n(paying)
    card_export(paying, minimum)
    paying.derive()
    before = figures(paying, N)
    assert before == (Decimal("25.00"), Decimal("775.00"), 0, "partial")

    statement_n1(paying, minimum)
    paying.derive()

    assert len(paying.conn.execute(select(payment)).all()) == 1
    assert applied(paying) == [Applied(N, Decimal("25.00"), True)]
    assert figures(paying, N) == (Decimal("25.00"), Decimal("775.00"), 0, "superseded")
    assert figures(paying, N1) == (0, Decimal("875.00"), Decimal("30.00"), "open")
    assert owed(paying)[N1].payments_mismatch is None


@pytest.mark.parametrize(
    ("day", "to"),
    [(D(2025, 3, 30), N), (STATEMENT_N1, N1), (D(2025, 4, 3), N1)],
    ids=["the day before N+1", "N+1's date", "after N+1"],
)
def test_only_a_payment_dated_on_or_after_the_next_statement_pays_its_minimum(
    paying: Ledger, day: dt.date, to: str
) -> None:
    minimum = paid_card(D(2025, 3, 25), "25.00")
    second = paid_card(day, "30.00")
    statement_n(paying)
    statement_n1(paying, minimum, *((second,) if day <= STATEMENT_N1 else ()))
    if day > STATEMENT_N1:
        paying.file("vancity-visa/2025-04-05.csv", visa_export(second))

    paying.derive()

    assert Applied(to, Decimal("30.00"), True) in applied(paying)
    threshold = figures(paying, N1)[2]
    assert threshold == (0 if to == N1 else Decimal("30.00"))


def test_a_card_payment_before_the_earliest_statement_has_no_target(
    paying: Ledger,
) -> None:
    """Printed on N, dated before N: it paid a statement that is not loaded."""
    statement_n(paying, paid_card(D(2025, 2, 20), "40.00"))
    paying.derive()

    (credit,) = paying.conn.execute(select(payment_application)).all()
    assert (credit.reason, credit.bound_on) == ("no_target", D(2025, 2, 20))
    assert figures(paying, N) == (0, Decimal("760.00"), Decimal("25.00"), "open")


@pytest.mark.parametrize(
    ("amount", "rows", "after"),
    [
        (
            "25.00",
            [Applied(N, Decimal("25.00"), True)],
            (Decimal("25.00"), Decimal("775.00"), 0, "partial"),
        ),
        (
            "850.00",
            [
                Applied(N, Decimal("800.00"), True),
                Applied("excess", Decimal("50.00"), True),
            ],
            (Decimal("800.00"), 0, 0, "paid"),
        ),
    ],
    ids=["the minimum", "more than the balance"],
)
def test_a_card_payment_fills_the_minimum_first_and_its_excess_stays_a_credit(
    paying: Ledger, amount: str, rows: list[Applied], after: tuple[Any, ...]
) -> None:
    """The next statement receives nothing of it: its balance already counts it."""
    paid = paid_card(D(2025, 3, 18), amount)
    statement_n(paying)
    statement_n1(paying, paid)

    paying.derive()

    assert applied(paying) == rows
    assert figures(paying, N) == (*after[:3], "superseded")
    assert owed(paying)[N1].paid == 0


@pytest.mark.parametrize(
    ("partial_kinds", "rows", "status"),
    [
        (None, [Applied(N, Decimal("100.00"), True)], "partial"),
        (
            '["arrangement_instalment"]',
            [Applied("confirm", Decimal("100.00"), True)],
            "open",
        ),
    ],
    ids=["default", "cards removed"],
)
def test_a_partial_card_payment_applies_only_while_cards_are_partial_kinds(
    paying: Ledger, partial_kinds: str | None, rows: list[Applied], status: str
) -> None:
    if partial_kinds is not None:
        with_settings(
            paying, f"[finance.payments]\npartial_auto_kinds = {partial_kinds}\n"
        )
    statement_n(paying)
    card_export(paying, paid_card(D(2025, 3, 10), "100.00"))

    paying.derive()

    assert applied(paying) == rows
    assert figures(paying, N)[3] == status


def test_a_payment_the_next_statement_does_not_print_is_a_payments_mismatch(
    paying: Ledger,
) -> None:
    """FR-21: an email says 40.00 reached the card on 20 March; N+1 prints only the
    25.00 line, so applied 65.00 and printed 25.00 differ by 40.00."""
    minimum = paid_card(D(2025, 3, 25), "25.00")
    statement_n(paying)
    statement_n1(paying, minimum)
    notice(paying, D(2025, 3, 20), amount=Decimal("40.00"), printed="Vancity Visa")

    paying.derive()

    assert owed(paying)[N1].payments_mismatch == Decimal("40.00")
    assert owed(paying)[N].payments_mismatch is None


# --- arrangements: a finished one stops setting the bills -----------------------------


def power(ledger: Ledger, *payments: tuple[dt.date, str]) -> None:
    """Two FortisBC bills, the older replaced by a two-instalment arrangement and the
    newer printing an arrears line, and the chequing payments of the instalments."""
    accounts(
        ledger,
        ("chequing", "Vancity", "chequing", CSV_IDENTIFIER, ""),
        ("hydro", "FortisBC", "bill_vendor", "9990001234", ""),
    )
    ledger.manual(PAYEES_FILE, "fortisbc,Our power,vendor,SAMPLE POWER,,")
    ledger.manual(
        ARRANGEMENTS_FILE,
        "plan,fortisbc,2026-01-10,1,50.00,2026-04-01,bill-2026-02",
        "plan,fortisbc,2026-01-10,2,60.00,2026-05-01,bill-2026-02",
    )
    ledger.file("fortisbc/a.pdf", fortis_bill("January 15, 2026", "February 5, 2026"))
    ledger.file(
        "fortisbc/b.pdf",
        fortis_with_arrears(fortis_bill("February 14, 2026", "March 7, 2026")),
    )
    file_bank(
        ledger,
        *on(CSV_IDENTIFIER, *((day, POWER, -Decimal(out)) for day, out in payments)),
    )


def test_a_finished_arrangement_stops_cutting_the_bills(ledger: Ledger) -> None:
    power(ledger, (D(2026, 3, 25), "50.00"), (D(2026, 4, 25), "60.00"))

    ledger.derive()

    found = owed(ledger)
    assert {label: found[label].status for label in found} == {
        "bill-2026-02": "superseded",
        "bill-2026-03": "open",
        "arrears-2026-02": "open",
        "inst-plan-1": "paid",
        "inst-plan-2": "paid",
    }
    assert found["bill-2026-02"].arrangement_ref == "plan"
    # The newest bill owes its total less its arrears, and the arrears are owed again.
    assert found["bill-2026-03"].amount_due == Decimal("297.60")
    assert found["arrears-2026-02"].amount_due == Decimal("100.00")


def test_an_arrangement_with_an_instalment_unpaid_still_sets_the_bills(
    ledger: Ledger,
) -> None:
    power(ledger, (D(2026, 3, 25), "50.00"))

    ledger.derive()

    found = owed(ledger)
    assert "arrears-2026-02" not in found
    assert found["bill-2026-03"].amount_due == Decimal("185.42")
    assert (found["inst-plan-1"].status, found["inst-plan-2"].status) == (
        "paid",
        "open",
    )
