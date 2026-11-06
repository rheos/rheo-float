"""Stage 15, the payments and what each was seen in (FR-22, Key decision 18; edge cases
15, 17, 34, 74 and 83; AC-25's construction half).

Seam: ``derive.run`` through the derive ``ledger`` over synthetic chequing and card
exports and notice documents, with the owner's decisions recorded through
``finance.decision.record``, read back as ``payment`` and ``payment_evidence``. Every
date, amount and name is invented.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import uuid
from collections.abc import Callable
from decimal import Decimal
from typing import Any, NamedTuple

import pytest
from sqlalchemy import insert, select, update
from sqlalchemy.exc import IntegrityError

from rheo_finance.context import UnitOfWork
from rheo_finance.decision import RecordOut
from rheo_finance.derive import coverage as coverage_stage
from rheo_finance.derive import fees, precedence, stage1, stage11
from rheo_finance.derive.run import run
from rheo_finance.derive.snapshot import txn_snapshot
from rheo_finance.manual.accounts import ACCOUNTS_FILE
from rheo_finance.manual.headers import MANUAL_FILES
from rheo_finance.manual.payees import PAYEES_FILE
from rheo_finance.manual.stage2 import account_id
from rheo_finance.manual.transfer_hints import TRANSFER_HINTS_FILE
from rheo_finance.storage.schema import (
    coverage,
    document,
    document_line,
    obligation,
    payee,
    payment,
    payment_application,
    payment_evidence,
    txn,
)

from .support import (
    CSV_IDENTIFIER,
    NOTICE_ISSUER,
    SECOND,
    D,
    Ledger,
    fx_lines,
    notice_lines,
    read_as_text,
)
from .test_invoice_decisions import AMOUNT_DUE, call, reverse, unpaid
from .test_receipts_invoices import (
    CONTRACTOR,
    INVOICE,
    PAID_OUTSIDE,
    RECEIPT,
    debit_only_receipt,
    invoice,
)
from .test_receipts_invoices import payments as chequing_export
from .test_transfer_edges import (
    BILL_PAYMENT,
    PAYMENT_RECEIVED,
    REFUND,
    file_bank,
    file_card,
    on,
    visa_export,
)

ISP = "PRE-AUTHORIZED DEBIT SAMPLE ISP 99911"
LANDLORD = "INTERAC E-TRANSFER  To: SAMPLE LANDLORD"
CANCEL = "E-TRANSFER DEBIT REVERSAL CUSTOMER CANCEL"
AMOUNT = Decimal("200.00")
BANK = Decimal("-200.00")
JUNE = D(2025, 6, 10)


class Seen(NamedTuple):
    """One payment as a test reads it: its evidence roles sorted."""

    payee: str
    paid_on: dt.date
    amount: Decimal
    source: str
    state: str
    roles: tuple[str, ...]


def seen(ledger: Ledger) -> list[Seen]:
    roles: dict[uuid.UUID, list[str]] = {}
    for row in ledger.conn.execute(select(payment_evidence)):
        roles.setdefault(row.payment_id, []).append(row.role)
    rows = ledger.conn.execute(
        select(payment, payee.c.slug).join(payee, payee.c.id == payment.c.payee_id)
    )
    return sorted(
        Seen(
            row.slug,
            row.paid_on,
            row.amount,
            row.source,
            row.state,
            tuple(sorted(roles[row.id])),
        )
        for row in rows
    )


def money(
    on_day: dt.date,
    *roles: str,
    name: str = "isp",
    amount: Decimal = AMOUNT,
    state: str = "active",
) -> Seen:
    return Seen(name, on_day, amount, "money", state, tuple(sorted(("money", *roles))))


def notice_only(on_day: dt.date, state: str, name: str = "isp") -> Seen:
    return Seen(name, on_day, AMOUNT, "notice_only", state, ("notice",))


def set_cell(ledger: Ledger, slug: str, column: str, value: str) -> None:
    """One cell of an account's row in the fixture's ``accounts.csv``."""
    path = ledger.data / "manual" / ACCOUNTS_FILE
    place = MANUAL_FILES[ACCOUNTS_FILE].index(column)
    rows = path.read_text(encoding="utf-8").splitlines()
    for index, row in enumerate(rows):
        if row.startswith(f"{slug},"):
            cells = row.split(",")
            cells[place] = value
            rows[index] = ",".join(cells)
    path.write_text("\n".join(rows) + "\n", encoding="utf-8")


def name_card(ledger: Ledger) -> None:
    """The ``own`` fixture's Visa takes the pattern its payment wording prints."""
    set_cell(ledger, "visa", "payee_patterns", "VANCITY VISA")


def vendors(ledger: Ledger) -> None:
    ledger.manual(
        PAYEES_FILE,
        "isp,Our net,vendor,SAMPLE ISP,,",
        "landlord,Our rent,person,SAMPLE LANDLORD,,",
    )


@pytest.fixture
def notices(own: Ledger, notice_parser: None) -> Ledger:
    """The ``own`` accounts, the vendors, the named card and the notice parser; a
    decision reads real lines."""
    vendors(own)
    name_card(own)
    own.ctx = dataclasses.replace(own.ctx, snapshot_provider=txn_snapshot)
    return own


def bank(ledger: Ledger, *rows: tuple[dt.date, str, Decimal]) -> None:
    file_bank(ledger, *on(CSV_IDENTIFIER, *rows))


number = iter(range(1000))


def notice(
    ledger: Ledger,
    day: dt.date,
    *,
    amount: Decimal = AMOUNT,
    currency: str = "CAD",
    kind: str = "payment_received",
    sender: str = "sender-a",
    printed: str = "Sample ISP",
) -> uuid.UUID:
    """A notice in a document of its own; the line's key, once derived, is what a
    decision names."""
    message = next(number)
    return ledger.file(
        f"{NOTICE_ISSUER}/{sender}/{message}.txt",
        notice_lines(f"{day} {amount} {currency} {kind} {printed}")
        + f"MESSAGE {message}\n",
    )


def notice_key(ledger: Ledger, document_id: uuid.UUID) -> uuid.UUID:
    key = ledger.conn.scalar(
        select(document_line.c.line_key_id).where(
            document_line.c.document_id == document_id
        )
    )
    assert isinstance(key, uuid.UUID)
    return key


def bank_line(ledger: Ledger, day: dt.date) -> uuid.UUID:
    found = ledger.conn.scalar(select(txn.c.id).where(txn.c.posting_date == day))
    assert isinstance(found, uuid.UUID)
    return found


def decide(ledger: Ledger, kind: str, key: uuid.UUID, line: uuid.UUID) -> uuid.UUID:
    out = call(
        ledger,
        "finance.decision.record",
        {
            "kind": kind,
            "target_ref": f"finance.payment_notice:{key}",
            "counterpart_ref": f"finance.txn:{line}",
        },
    )
    assert isinstance(out, RecordOut)
    return out.decision_id


# --- the stated fixtures --------------------------------------------------------------


def test_one_payment_seen_as_bank_line_card_credit_and_notice_is_one(
    notices: Ledger,
) -> None:
    bank(notices, (D(2025, 6, 2), BILL_PAYMENT, BANK))
    file_card(notices, (D(2025, 6, 4), PAYMENT_RECEIVED, AMOUNT))
    notice(notices, D(2025, 6, 5), printed="Vancity Visa")

    notices.derive()

    assert seen(notices) == [money(D(2025, 6, 4), "joined", "notice", name="visa")]


def test_the_card_credit_is_the_money_line_and_the_debit_is_joined(
    notices: Ledger,
) -> None:
    bank(notices, (D(2025, 6, 2), BILL_PAYMENT, BANK))
    file_card(notices, (D(2025, 6, 4), PAYMENT_RECEIVED, AMOUNT))

    notices.derive()

    rows = {row.role: row for row in notices.conn.execute(select(payment_evidence))}
    assert rows["money"].txn_id == bank_line(notices, D(2025, 6, 4))
    assert rows["joined"].txn_id == bank_line(notices, D(2025, 6, 2))
    assert rows["money"].stream_key == f"account:{account_id('visa')}"
    assert rows["joined"].stream_key == f"account:{account_id('chequing')}"


def test_two_equal_same_day_payments_from_one_account_and_one_email_are_two(
    notices: Ledger,
) -> None:
    bank(notices, (JUNE, ISP, BANK), (JUNE, ISP, BANK))
    notice(notices, JUNE)

    notices.derive()

    assert seen(notices) == [money(JUNE), money(JUNE, "notice")]


def test_two_equal_payments_from_two_accounts_three_days_apart_are_two(
    notices: Ledger,
) -> None:
    file_bank(
        notices,
        *on(CSV_IDENTIFIER, (JUNE, ISP, BANK)),
        *on(SECOND, (D(2025, 6, 13), ISP, BANK)),
    )

    notices.derive()

    assert seen(notices) == [money(JUNE), money(D(2025, 6, 13))]


def test_two_equal_debits_to_a_card_with_two_credits_pair_in_date_order_as_two(
    notices: Ledger,
) -> None:
    bank(
        notices,
        (D(2025, 6, 2), BILL_PAYMENT, BANK),
        (D(2025, 6, 3), BILL_PAYMENT, BANK),
    )
    file_card(
        notices,
        (D(2025, 6, 4), PAYMENT_RECEIVED, AMOUNT),
        (D(2025, 6, 8), PAYMENT_RECEIVED, AMOUNT),
    )

    notices.derive()

    assert seen(notices) == [
        money(D(2025, 6, 4), "joined", name="visa"),
        money(D(2025, 6, 8), "joined", name="visa"),
    ]


def test_with_the_second_credit_missing_the_unpaired_debit_is_confirm_only(
    notices: Ledger,
) -> None:
    """The second debit is outside the first credit's window: the first pairs alone."""
    bank(
        notices,
        (D(2025, 6, 2), BILL_PAYMENT, BANK),
        (D(2025, 6, 20), BILL_PAYMENT, BANK),
    )
    file_card(notices, (D(2025, 6, 4), PAYMENT_RECEIVED, AMOUNT))

    notices.derive()

    assert seen(notices) == [
        money(D(2025, 6, 4), "joined", name="visa"),
        money(D(2025, 6, 20), name="visa", state="confirm_only"),
    ]


def test_two_close_debits_and_one_credit_leave_both_debits_confirm_only(
    notices: Ledger,
) -> None:
    """Stage 9 cannot say which debit it was, so no edge applies: the credit is the
    payment, and each debit is a question, never a second payment."""
    bank(
        notices,
        (D(2025, 6, 2), BILL_PAYMENT, BANK),
        (D(2025, 6, 3), BILL_PAYMENT, BANK),
    )
    file_card(notices, (D(2025, 6, 4), PAYMENT_RECEIVED, AMOUNT))

    notices.derive()

    assert seen(notices) == [
        money(D(2025, 6, 2), name="visa", state="confirm_only"),
        money(D(2025, 6, 3), name="visa", state="confirm_only"),
        money(D(2025, 6, 4), name="visa"),
    ]


def test_an_email_eight_days_from_its_bank_line_is_held(notices: Ledger) -> None:
    bank(notices, (JUNE, ISP, BANK))
    notice(notices, D(2025, 6, 18))

    notices.derive()

    assert seen(notices) == [
        money(JUNE),
        notice_only(D(2025, 6, 18), "confirm_only"),
    ]


def test_a_reversed_money_line_is_a_reversed_payment_and_takes_its_notice(
    notices: Ledger,
) -> None:
    bank(
        notices,
        (JUNE, LANDLORD, BANK),
        (D(2025, 6, 12), CANCEL, AMOUNT),
    )
    notice(notices, D(2025, 6, 11), printed="Sample Landlord")

    notices.derive()

    assert seen(notices) == [
        money(JUNE, "notice", name="landlord", state="reversed"),
    ]


def test_the_reversal_of_a_clawed_back_credit_is_no_payment(notices: Ledger) -> None:
    """A reversal debit undoes money received. It left the account, and pays no one,
    even when its wording names a payee."""
    bank(
        notices,
        (JUNE, "DEPOSIT SAMPLE LANDLORD", AMOUNT),
        (D(2025, 6, 12), "CREDIT REVERSAL SAMPLE LANDLORD", BANK),
    )

    notices.derive()

    assert seen(notices) == []


def test_a_reversal_debit_with_no_original_is_no_payment(notices: Ledger) -> None:
    bank(notices, (D(2025, 6, 12), "CREDIT REVERSAL SAMPLE LANDLORD", BANK))

    notices.derive()

    assert seen(notices) == []


def test_a_reversed_card_credit_is_a_reversed_payment(notices: Ledger) -> None:
    """Stage 8 took the credit out of stage 9's reading, so only the parser's wording
    says it was a payment. The owner's row marks the wording of the card's reversal."""
    notices.manual(TRANSFER_HINTS_FILE, "payment reversal,reversal,")
    file_card(
        notices,
        (JUNE, PAYMENT_RECEIVED, AMOUNT),
        (D(2025, 6, 12), "PAYMENT REVERSAL", BANK),
    )

    notices.derive()

    assert seen(notices) == [money(JUNE, name="visa", state="reversed")]


# --- a card ---------------------------------------------------------------------------


def test_a_debit_naming_a_card_with_no_card_lines_at_all_is_the_payment(
    notices: Ledger,
) -> None:
    bank(notices, (JUNE, BILL_PAYMENT, BANK))

    notices.derive()

    assert seen(notices) == [money(JUNE, name="visa")]


def test_a_debit_naming_a_card_in_a_month_the_card_is_missing_is_the_payment(
    notices: Ledger,
) -> None:
    """The card has lines for April and June, none for May."""
    bank(notices, (D(2025, 5, 12), BILL_PAYMENT, BANK))
    for name, day in (("04-30", D(2025, 4, 2)), ("06-30", D(2025, 6, 2))):
        notices.file(
            f"vancity-visa/2025-{name}.csv",
            visa_export((day, "SAMPLE SHOP", Decimal("-10.00"))),
        )

    notices.derive()

    state = notices.conn.scalar(
        select(coverage.c.state).where(
            coverage.c.account_id == account_id("visa"),
            coverage.c.month == D(2025, 5, 1),
        )
    )
    assert state == "missing"
    assert seen(notices) == [money(D(2025, 5, 12), name="visa")]


def test_a_debit_naming_a_card_in_a_month_the_card_has_lines_is_confirm_only(
    notices: Ledger,
) -> None:
    bank(notices, (JUNE, BILL_PAYMENT, BANK))
    file_card(notices, (D(2025, 6, 2), "SAMPLE SHOP", Decimal("-10.00")))

    notices.derive()

    assert seen(notices) == [money(JUNE, name="visa", state="confirm_only")]


def test_a_debit_naming_a_card_with_no_transfer_wording_is_still_asked_about(
    notices: Ledger,
) -> None:
    """It has no state at all, and nothing joined it: it must not vanish."""
    bank(notices, (JUNE, "CHEQUE VANCITY VISA", BANK))
    file_card(notices, (D(2025, 6, 2), "SAMPLE SHOP", Decimal("-10.00")))

    notices.derive()

    assert seen(notices) == [money(JUNE, name="visa", state="confirm_only")]


def test_a_card_refund_is_no_payment_and_a_card_payment_with_no_debit_is(
    notices: Ledger,
) -> None:
    file_card(
        notices,
        (D(2025, 6, 2), REFUND, AMOUNT),
        (D(2025, 6, 4), PAYMENT_RECEIVED, Decimal("90.00")),
    )

    notices.derive()

    assert seen(notices) == [money(D(2025, 6, 4), name="visa", amount=Decimal("90.00"))]


def test_a_purchase_on_a_card_that_names_a_vendor_pays_the_vendor(
    notices: Ledger,
) -> None:
    file_card(notices, (JUNE, ISP, BANK))

    notices.derive()

    assert seen(notices) == [money(JUNE)]


# --- own transfers --------------------------------------------------------------------

SWEEP = "SAMPLE SAVINGS SWEEP 99976"


@pytest.mark.parametrize("named", ["vendor", "own account"])
def test_a_joined_transfer_between_own_accounts_is_no_payment(
    notices: Ledger, named: str
) -> None:
    """No leg leaves the owner's accounts and none reaches a card, so the debit's
    wording naming a payee pays nobody."""
    if named == "vendor":
        notices.manual(PAYEES_FILE, "sweeper,Our sweep,vendor,SAMPLE SAVINGS,,")
    else:
        set_cell(notices, "second", "payee_patterns", "SAMPLE SAVINGS")
    notices.manual(TRANSFER_HINTS_FILE, "sample savings sweep,out,")
    file_bank(
        notices,
        *on(CSV_IDENTIFIER, (D(2025, 6, 2), SWEEP, Decimal("-300.00"))),
        *on(SECOND, (D(2025, 6, 3), "DEPOSIT SAMPLE SWEEP 99977", Decimal("300.00"))),
    )

    notices.derive()

    assert {row["transfer_state"] for row in notices.txns()} == {"paired"}
    assert seen(notices) == []


def test_a_debit_naming_a_payee_that_stands_for_an_own_account_is_no_payment(
    notices: Ledger,
) -> None:
    set_cell(notices, "second", "payee_patterns", "SAMPLE SAVINGS")
    bank(notices, (JUNE, SWEEP, BANK))

    notices.derive()

    assert seen(notices) == []


def test_a_debit_naming_a_bill_vendor_accounts_payee_pays_that_vendor(
    notices: Ledger,
) -> None:
    manual = notices.data / "manual"
    with (manual / ACCOUNTS_FILE).open("a", encoding="utf-8") as out:
        out.write(
            "mobile,Mobile,Sample Mobile,bill_vendor,CAD,monthly,,,,,,"
            "SAMPLE MOBILE,,,\n"
        )
    notices.add_identifier("mobile", "999000123")
    bank(notices, (JUNE, "PRE-AUTHORIZED DEBIT SAMPLE MOBILE 99912", BANK))

    notices.derive()

    assert seen(notices) == [money(JUNE, name="mobile")]


# --- which lines ----------------------------------------------------------------------


def test_a_credit_that_names_a_vendor_is_no_payment(notices: Ledger) -> None:
    bank(notices, (JUNE, "REFUND SAMPLE ISP", AMOUNT))

    notices.derive()

    assert seen(notices) == []


def test_a_line_that_names_no_payee_is_no_payment(notices: Ledger) -> None:
    bank(notices, (JUNE, "POS PURCHASE SAMPLE SHOP", BANK))

    notices.derive()

    assert seen(notices) == []


def test_a_line_two_payees_name_is_one_payment_to_the_first(own: Ledger) -> None:
    own.manual(
        PAYEES_FILE,
        "isp,Our net,vendor,SAMPLE ISP,,",
        "other,Other net,vendor,PRE-AUTHORIZED DEBIT,,",
    )
    bank(own, (JUNE, ISP, BANK))

    own.derive()

    assert seen(own) == [money(JUNE)]


def test_a_payee_with_no_patterns_is_named_by_nothing(ledger: Ledger) -> None:
    """The invoice directory's vendor payee has no patterns."""
    chequing_export(ledger, ("10-Jun-2025", CONTRACTOR, "200.00"))

    ledger.derive()

    assert seen(ledger) == []


@pytest.mark.parametrize("placeholder", [False, True])
def test_an_excluded_line_is_no_payment(notices: Ledger, placeholder: bool) -> None:
    """Every line of a placeholder account is excluded from every total."""
    if placeholder:
        set_cell(notices, "fx", "kind", "crypto_wallet")
        set_cell(notices, "fx", "placeholder", "true")
    notices.file(
        "synthetic-fx/lines.txt",
        fx_lines("CAD", "2025-06-10 -200.00 - - SAMPLE ISP 99911"),
    )

    notices.derive()

    assert seen(notices) == ([] if placeholder else [money(JUNE)])


def test_a_fee_line_that_names_a_payee_is_no_payment(notices: Ledger) -> None:
    """A parser prints a payment's fee under the payment's wording. The stored line is
    changed, because no chequing wording here is a fee."""
    bank(notices, (JUNE, ISP, BANK))
    notices.derive()
    notices.conn.execute(document_line.update().values(fee_type="service_fee"))

    notices.derive()

    assert seen(notices) == []


def test_a_transfers_derived_fee_line_is_no_payment(notices: Ledger) -> None:
    """A debit and a card credit that differ book a fee line under the debit's account,
    in wording a payee could name. The payment is what reached the card."""
    notices.manual(PAYEES_FILE, "bank,Our bank,vendor,FEE,,")
    bank(notices, (D(2025, 6, 2), BILL_PAYMENT, Decimal("-203.00")))
    file_card(notices, (D(2025, 6, 4), PAYMENT_RECEIVED, AMOUNT))

    notices.derive()

    assert any(row["origin"] == fees.FEE_ORIGIN for row in notices.txns())
    assert seen(notices) == [money(D(2025, 6, 4), "joined", name="visa")]


def test_a_receipts_txn_is_named_by_its_printed_payee(own: Ledger) -> None:
    """A cash receipt is money that left, on the internal account."""
    own.manual(PAYEES_FILE, "clinic,Our dentist,vendor,SAMPLE DENTAL CLINIC,,")
    own.file(RECEIPT, debit_only_receipt())

    own.derive()

    assert seen(own) == [
        money(D(2026, 3, 15), name="clinic", amount=Decimal("45.00")),
    ]
    (row,) = own.conn.execute(select(payment_evidence)).all()
    assert row.stream_key == f"account:{PAID_OUTSIDE}"


# --- notices --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("after", "joins"),
    [(5, True), (6, False), (-5, True), (-6, False)],
    ids=["five after", "six after", "five before", "six before"],
)
def test_a_notice_joins_inside_the_evidence_window(
    notices: Ledger, after: int, joins: bool
) -> None:
    bank(notices, (JUNE, ISP, BANK))
    day = JUNE + dt.timedelta(days=after)
    notice(notices, day)

    notices.derive()

    assert seen(notices) == (
        [money(JUNE, "notice")]
        if joins
        else sorted([money(JUNE), notice_only(day, "confirm_only")])
    )


@pytest.mark.parametrize(
    ("after", "state"),
    [(31, "confirm_only"), (32, "active"), (-31, "confirm_only"), (-32, "active")],
    ids=["31 after", "32 after", "31 before", "32 before"],
)
def test_a_leftover_notice_is_held_inside_the_hold_window_and_its_own_payment_outside(
    notices: Ledger, after: int, state: str
) -> None:
    bank(notices, (JUNE, ISP, BANK))
    day = JUNE + dt.timedelta(days=after)
    notice(notices, day)

    notices.derive()

    assert seen(notices) == sorted([money(JUNE), notice_only(day, state)])


def test_a_notice_with_no_bank_line_is_an_active_payment_of_its_own(
    notices: Ledger,
) -> None:
    notice(notices, JUNE)

    notices.derive()

    assert seen(notices) == [notice_only(JUNE, "active")]


@pytest.mark.parametrize(
    ("after", "state"),
    [(0, "confirm_only"), (31, "confirm_only"), (32, "active")],
    ids=["same day", "31 after", "32 after"],
)
def test_a_second_streams_notice_of_a_notice_only_payment_is_held(
    notices: Ledger, after: int, state: str
) -> None:
    """Two emails about one payment and no bank line yet: it counts once."""
    notice(notices, JUNE, sender="sender-b")
    day = JUNE + dt.timedelta(days=after)
    notice(notices, day, kind="etransfer")

    notices.derive()

    assert seen(notices) == sorted(
        [notice_only(JUNE, "active"), notice_only(day, state)]
    )


def test_a_second_notice_of_the_same_stream_is_a_second_payment(
    notices: Ledger,
) -> None:
    notice(notices, JUNE)
    notice(notices, D(2025, 6, 20))

    notices.derive()

    assert seen(notices) == [
        notice_only(JUNE, "active"),
        notice_only(D(2025, 6, 20), "active"),
    ]


def test_when_the_statement_loads_its_line_joins_the_notice_and_the_count_stays_one(
    notices: Ledger,
) -> None:
    notice(notices, JUNE)
    notices.derive()
    assert seen(notices) == [notice_only(JUNE, "active")]

    bank(notices, (D(2025, 6, 12), ISP, BANK))
    notices.derive()

    assert seen(notices) == [money(D(2025, 6, 12), "notice")]


def test_when_the_statement_loads_outside_the_window_the_notice_is_held(
    notices: Ledger,
) -> None:
    notice(notices, JUNE)
    notices.derive()

    bank(notices, (D(2025, 6, 27), ISP, BANK))
    notices.derive()

    assert seen(notices) == [
        notice_only(JUNE, "confirm_only"),
        money(D(2025, 6, 27)),
    ]


@pytest.mark.parametrize(
    ("printed", "joins"),
    [("200.01", True), ("199.99", True), ("200.02", False), ("199.98", False)],
)
def test_a_notice_joins_within_the_match_tolerance(
    notices: Ledger, printed: str, joins: bool
) -> None:
    bank(notices, (JUNE, ISP, BANK))
    notice(notices, JUNE, amount=Decimal(printed))

    notices.derive()

    found = seen(notices)
    assert (found == [money(JUNE, "notice")]) is joins
    # Beyond the tolerance it is no equal payment, so it is not held for one either.
    if not joins:
        assert [each.state for each in found] == ["active", "active"]


def test_a_notice_in_another_currency_is_not_that_payment(notices: Ledger) -> None:
    bank(notices, (JUNE, ISP, BANK))
    notice(notices, JUNE, currency="USD")

    notices.derive()

    assert sorted((each.source, each.state) for each in seen(notices)) == [
        ("money", "active"),
        ("notice_only", "active"),
    ]


def test_the_closest_date_takes_the_notice(notices: Ledger) -> None:
    bank(
        notices,
        (D(2025, 6, 8), ISP, BANK),
        (D(2025, 6, 12), ISP, BANK),
    )
    notice(notices, D(2025, 6, 11))

    notices.derive()

    assert seen(notices) == [money(D(2025, 6, 8)), money(D(2025, 6, 12), "notice")]


def test_two_streams_both_join_one_payment(notices: Ledger) -> None:
    bank(notices, (JUNE, ISP, BANK))
    notice(notices, JUNE)
    notice(notices, JUNE, kind="etransfer")
    notice(notices, JUNE, sender="sender-b")

    notices.derive()

    assert seen(notices) == [money(JUNE, "notice", "notice", "notice")]
    streams = notices.conn.scalars(
        select(payment_evidence.c.stream_key).where(payment_evidence.c.role == "notice")
    ).all()
    assert sorted(streams) == [
        f"notice:{NOTICE_ISSUER}/sender-a:etransfer",
        f"notice:{NOTICE_ISSUER}/sender-a:payment_received",
        f"notice:{NOTICE_ISSUER}/sender-b:payment_received",
    ]


def test_a_second_notice_of_one_stream_is_held_not_a_second_join(
    notices: Ledger,
) -> None:
    bank(notices, (JUNE, ISP, BANK))
    notice(notices, JUNE)
    notice(notices, D(2025, 6, 11))

    notices.derive()

    assert seen(notices) == [
        money(JUNE, "notice"),
        notice_only(D(2025, 6, 11), "confirm_only"),
    ]


def test_copies_of_one_notice_are_one_notice(notices: Ledger) -> None:
    text = notice_lines(f"{JUNE} 200.00 CAD payment_received Sample ISP")
    notices.file(f"{NOTICE_ISSUER}/sender-a/one.txt", text)
    notices.file(f"{NOTICE_ISSUER}/sender-a/two.txt", text + "\n")

    notices.derive()

    assert seen(notices) == [notice_only(JUNE, "active")]


def test_two_like_notices_of_one_document_are_two(notices: Ledger) -> None:
    row = f"{JUNE} 200.00 CAD payment_received Sample ISP"
    notices.file(f"{NOTICE_ISSUER}/sender-a/one.txt", notice_lines(row, row))

    notices.derive()

    assert seen(notices) == [notice_only(JUNE, "active"), notice_only(JUNE, "active")]


def test_a_notice_that_names_no_payee_makes_no_payment(notices: Ledger) -> None:
    notice(notices, JUNE, printed="Sample Unknown")

    notices.derive()

    assert seen(notices) == []


def test_a_notice_is_named_by_its_description_when_its_printed_payee_names_none(
    notices: Ledger,
) -> None:
    notices.manual(PAYEES_FILE, "isp,Our net,vendor,PAYMENT RECEIVED,,")
    notice(notices, JUNE, printed="Sample Unknown")

    notices.derive()

    assert seen(notices) == [notice_only(JUNE, "active")]


# --- the owner's decisions ------------------------------------------------------------


def test_pair_joins_a_held_notice_and_reversing_it_holds_the_notice_again(
    notices: Ledger,
) -> None:
    bank(notices, (JUNE, ISP, BANK))
    document = notice(notices, D(2025, 6, 18))
    notices.derive()
    held = seen(notices)

    decision_id = decide(
        notices, "pair", notice_key(notices, document), bank_line(notices, JUNE)
    )
    notices.derive()
    assert seen(notices) == [money(JUNE, "notice")]

    reverse(notices, decision_id)
    notices.derive()
    assert seen(notices) == held


def test_pair_joins_whatever_the_amount_and_date_say(notices: Ledger) -> None:
    bank(notices, (JUNE, ISP, BANK))
    document = notice(
        notices, D(2025, 9, 1), amount=Decimal("15.00"), printed="Sample Unknown"
    )
    notices.derive()

    decide(notices, "pair", notice_key(notices, document), bank_line(notices, JUNE))
    notices.derive()

    assert seen(notices) == [money(JUNE, "notice")]


def test_pair_names_the_debit_of_a_joined_payment(notices: Ledger) -> None:
    """A decision names evidence, not a payment, so the debit reaches the card's."""
    bank(notices, (D(2025, 6, 2), BILL_PAYMENT, BANK))
    file_card(notices, (D(2025, 6, 4), PAYMENT_RECEIVED, AMOUNT))
    document = notice(notices, D(2025, 7, 20), printed="Vancity Visa")
    notices.derive()
    assert len(seen(notices)) == 2

    decide(
        notices,
        "pair",
        notice_key(notices, document),
        bank_line(notices, D(2025, 6, 2)),
    )
    notices.derive()

    assert seen(notices) == [money(D(2025, 6, 4), "joined", "notice", name="visa")]


def test_pair_may_add_a_second_notice_of_a_stream_and_the_rule_then_adds_no_third(
    notices: Ledger,
) -> None:
    bank(notices, (JUNE, ISP, BANK))
    first = notice(notices, D(2025, 6, 25))
    notice(notices, D(2025, 6, 11))
    notices.derive()

    decide(notices, "pair", notice_key(notices, first), bank_line(notices, JUNE))
    notices.derive()

    assert seen(notices) == [
        money(JUNE, "notice"),
        notice_only(D(2025, 6, 11), "confirm_only"),
    ]


def test_unpair_makes_a_notice_its_own_payment(notices: Ledger) -> None:
    bank(notices, (JUNE, ISP, BANK))
    document = notice(notices, JUNE)
    notices.derive()
    assert seen(notices) == [money(JUNE, "notice")]

    decision_id = decide(
        notices, "unpair", notice_key(notices, document), bank_line(notices, JUNE)
    )
    notices.derive()
    assert seen(notices) == sorted([money(JUNE), notice_only(JUNE, "active")])

    reverse(notices, decision_id)
    notices.derive()
    assert seen(notices) == [money(JUNE, "notice")]


def test_unpair_leaves_the_notice_free_for_another_equal_payment(
    notices: Ledger,
) -> None:
    bank(notices, (JUNE, ISP, BANK), (D(2025, 6, 12), ISP, BANK))
    document = notice(notices, JUNE)
    notices.derive()
    assert seen(notices) == [money(JUNE, "notice"), money(D(2025, 6, 12))]

    decide(notices, "unpair", notice_key(notices, document), bank_line(notices, JUNE))
    notices.derive()

    assert seen(notices) == [money(JUNE), money(D(2025, 6, 12), "notice")]


def test_the_newest_decision_on_a_notice_and_a_line_holds(notices: Ledger) -> None:
    bank(notices, (JUNE, ISP, BANK))
    document = notice(notices, D(2025, 6, 18))
    notices.derive()
    key, line = notice_key(notices, document), bank_line(notices, JUNE)

    decide(notices, "pair", key, line)
    decide(notices, "unpair", key, line)
    notices.derive()
    assert seen(notices) == sorted([money(JUNE), notice_only(D(2025, 6, 18), "active")])

    decide(notices, "pair", key, line)
    notices.derive()
    assert seen(notices) == [money(JUNE, "notice")]


def test_a_pair_whose_line_is_no_payment_leaves_the_notice_as_it_was(
    notices: Ledger,
) -> None:
    bank(notices, (JUNE, "POS PURCHASE SAMPLE SHOP", BANK))
    document = notice(notices, JUNE)
    notices.derive()

    decide(notices, "pair", notice_key(notices, document), bank_line(notices, JUNE))
    notices.derive()

    assert seen(notices) == [notice_only(JUNE, "active")]


def test_a_decision_on_a_notice_is_not_read_by_the_pairing_stages(
    notices: Ledger,
) -> None:
    bank(notices, (D(2025, 6, 2), BILL_PAYMENT, BANK))
    file_card(notices, (D(2025, 6, 4), PAYMENT_RECEIVED, AMOUNT))
    document = notice(notices, D(2025, 6, 2), printed="Vancity Visa")
    notices.derive()
    before = notices.txns()

    decide(
        notices,
        "unpair",
        notice_key(notices, document),
        bank_line(notices, D(2025, 6, 2)),
    )
    notices.derive()

    assert notices.txns() == before
    assert seen(notices) == sorted(
        [
            money(D(2025, 6, 4), "joined", name="visa"),
            notice_only(D(2025, 6, 2), "active", name="visa"),
        ]
    )


# --- what the tables hold -------------------------------------------------------------


def test_a_derive_and_a_rebuild_give_the_same_rows(notices: Ledger) -> None:
    bank(notices, (JUNE, ISP, BANK), (D(2025, 7, 1), ISP, BANK))
    file_card(notices, (D(2025, 6, 4), PAYMENT_RECEIVED, AMOUNT))
    notice(notices, JUNE)
    notice(notices, D(2025, 7, 9))
    notices.derive()

    def state() -> tuple[list[Any], list[Any]]:
        return (
            sorted(repr(tuple(row)) for row in notices.conn.execute(select(payment))),
            sorted(
                repr(tuple(row))
                for row in notices.conn.execute(select(payment_evidence))
            ),
        )

    derived = state()
    notices.derive()
    assert state() == derived
    run(notices.uow, notices.ctx, extract_pdf=read_as_text, reparse=True)
    assert state() == derived


@pytest.mark.parametrize("both", [False, True], ids=["none", "two"])
def test_evidence_names_exactly_one_thing(notices: Ledger, both: bool) -> None:
    bank(notices, (JUNE, ISP, BANK))
    notice(notices, D(2025, 6, 18))
    notices.derive()
    notice_line = notices.conn.scalar(
        select(document_line.c.id).where(document_line.c.line_kind == "payment_notice")
    )
    named = {"txn_id": uuid.uuid4(), "document_line_id": notice_line} if both else {}
    with pytest.raises(IntegrityError, match="check"), notices.conn.begin_nested():
        notices.conn.execute(
            insert(payment_evidence).values(
                id=uuid.uuid4(),
                payment_id=notices.conn.scalar(select(payment.c.id)),
                stream_key="account:x",
                role="joined",
                **named,
            )
        )


@pytest.mark.parametrize(
    ("values", "stored"),
    [
        ({"reason": "payment_unapply"}, True),
        ({"reason": "confirm"}, True),
        ({"reason": "no_target", "bound_on": JUNE}, True),
        ({"reason": "no_target"}, False),
        ({"reason": "excess", "bound_on": JUNE}, False),
        ({"reason": "rolled_forward"}, False),
    ],
    ids=["unapplied", "confirm", "no target", "no bound", "bound on excess", "other"],
)
def test_an_unapplied_credit_names_an_honest_reason(
    notices: Ledger, values: dict[str, object], stored: bool
) -> None:
    bank(notices, (JUNE, ISP, BANK))
    notices.derive()
    row = {
        "id": uuid.uuid4(),
        "payment_id": notices.conn.scalar(select(payment.c.id)),
        "amount": Decimal("0.125"),
        "auto": False,
        **values,
    }
    if stored:
        notices.conn.execute(insert(payment_application).values(**row))
        assert notices.conn.scalar(
            select(payment_application.c.amount).where(
                payment_application.c.id == row["id"]
            )
        ) == Decimal("0.125")
        return
    with pytest.raises(IntegrityError, match="check"), notices.conn.begin_nested():
        notices.conn.execute(insert(payment_application).values(**row))


# --- the cases each rule turns on -----------------------------------------------------


@pytest.mark.parametrize(
    ("state", "paid_state"),
    [
        ("unparsed", "active"),
        ("rejected", "active"),
        ("missing", "active"),
        ("partial", "confirm_only"),
        ("have", "confirm_only"),
    ],
)
def test_a_debit_naming_a_card_is_the_payment_only_in_a_month_with_no_card_lines(
    notices: Ledger,
    monkeypatch: pytest.MonkeyPatch,
    state: str,
    paid_state: str,
) -> None:
    """The card's June is set to each state as the coverage stage would leave it."""
    original: Callable[..., Any] = coverage_stage.build_coverage

    def then_set(uow: UnitOfWork, *args: Any) -> Any:
        months = original(uow, *args)
        uow.execute(
            update(coverage)
            .where(
                coverage.c.account_id == account_id("visa"),
                coverage.c.month == D(2025, 6, 1),
            )
            .values(state=state, reason_code=None)
        )
        return months

    monkeypatch.setattr(coverage_stage, "build_coverage", then_set)
    bank(notices, (JUNE, BILL_PAYMENT, BANK))
    file_card(notices, (D(2025, 6, 2), "SAMPLE SHOP", Decimal("-10.00")))

    notices.derive()

    assert seen(notices) == [money(JUNE, name="visa", state=paid_state)]


def test_a_notice_printed_negative_is_the_payment_of_its_size(notices: Ledger) -> None:
    bank(notices, (JUNE, ISP, BANK))
    notice(notices, JUNE, amount=BANK)

    notices.derive()

    assert seen(notices) == [money(JUNE, "notice")]


def test_a_notice_in_a_document_that_is_not_parsed_is_no_notice(
    notices: Ledger, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Its lines stay stored; the document no longer counts."""
    filed = notice(notices, JUNE)
    original: Callable[[UnitOfWork], int] = precedence.supersede

    def then_set_aside(uow: UnitOfWork) -> int:
        made = original(uow)
        uow.execute(
            update(document).where(document.c.id == filed).values(status="superseded")
        )
        return made

    monkeypatch.setattr(stage1, "supersede", then_set_aside)

    notices.derive()

    assert notices.conn.scalar(
        select(document_line.c.id).where(document_line.c.document_id == filed)
    )
    assert seen(notices) == []


def test_the_copy_of_the_larger_sha256_stands_for_a_notice(notices: Ledger) -> None:
    text = notice_lines(f"{JUNE} 200.00 CAD payment_received Sample ISP")
    filed = [
        notices.file(f"{NOTICE_ISSUER}/sender-a/{name}.txt", text + "\n" * extra)
        for name, extra in (("one", 0), ("two", 1), ("three", 2))
    ]
    notices.derive()

    shas = dict(
        notices.conn.execute(
            select(document.c.id, document.c.sha256).where(document.c.id.in_(filed))
        ).all()
    )
    standing = notices.conn.scalar(
        select(document_line.c.id).where(
            document_line.c.document_id == max(filed, key=lambda each: shas[each])
        )
    )
    (row,) = notices.conn.execute(select(payment_evidence)).all()
    assert row.document_line_id == standing


@pytest.mark.parametrize("kind", ["conversion", "trade", "loan"])
def test_a_line_paired_as_anything_but_an_own_transfer_is_no_payment(
    notices: Ledger, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    original: Callable[..., None] = stage11.resolve

    def then_pair(uow: UnitOfWork, *args: Any) -> None:
        original(uow, *args)
        uow.execute(update(txn).values(pair_kind=kind))

    monkeypatch.setattr(stage11, "resolve", then_pair)
    bank(notices, (JUNE, ISP, BANK))

    notices.derive()

    assert seen(notices) == []


def test_a_debit_on_the_card_itself_naming_the_card_pays_nothing(
    notices: Ledger,
) -> None:
    """A returned payment, say: it took money back off the card."""
    file_card(notices, (JUNE, "RETURNED PAYMENT VANCITY VISA", BANK))

    notices.derive()

    assert seen(notices) == []


def test_a_fans_amount_is_the_sum_of_its_credits(notices: Ledger) -> None:
    """One debit the owner pairs with two card credits is one payment of both."""
    bank(notices, (D(2025, 6, 2), BILL_PAYMENT, Decimal("-400.00")))
    file_card(
        notices,
        (D(2025, 6, 4), PAYMENT_RECEIVED, AMOUNT),
        (D(2025, 6, 5), PAYMENT_RECEIVED, AMOUNT),
    )
    notices.derive()

    call(
        notices,
        "finance.decision.record",
        {
            "kind": "pair",
            "target_ref": f"finance.txn:{bank_line(notices, D(2025, 6, 2))}",
            "legs": [
                {"target_ref": f"finance.txn:{bank_line(notices, D(2025, 6, 4))}"},
                {"target_ref": f"finance.txn:{bank_line(notices, D(2025, 6, 5))}"},
            ],
        },
    )
    notices.derive()

    assert seen(notices) == [
        money(
            D(2025, 6, 4),
            "joined",
            "joined",
            name="visa",
            amount=Decimal("400.00"),
        )
    ]


# --- an invoice debt ------------------------------------------------------------------


def invoice_debt(ledger: Ledger, *rows: tuple[str, str, str]) -> None:
    """The contractor's invoice, owed to a payee that names the printed name, decided
    unpaid, beside a chequing export of ``rows``. The invoice prints a total of 750.00
    and an amount due of 650.00."""
    ledger.manual(PAYEES_FILE, "builder,Our builder,vendor,SAMPLE CONTRACTOR,,")
    chequing_export(ledger, *rows)
    ledger.file(INVOICE, invoice())
    ledger.derive()
    unpaid(ledger)
    ledger.derive()


def test_a_payment_of_an_invoice_debts_amount_due_is_a_payment_at_its_remaining(
    ledger: Ledger,
) -> None:
    """No total matches it in stage 3, so the debt stands and the payment reaches it
    with the very amount application compares."""
    invoice_debt(ledger, ("10-Mar-2026", CONTRACTOR, "650.00"))

    (debt,) = ledger.conn.execute(select(obligation)).all()
    (paid,) = seen(ledger)
    builder = ledger.conn.scalar(select(payee.c.id).where(payee.c.slug == "builder"))
    assert debt.payee_id == builder
    assert debt.amount_due == AMOUNT_DUE
    assert paid == money(D(2026, 3, 10), name="builder", amount=AMOUNT_DUE)


def test_a_payment_of_an_invoice_debts_amount_due_clears_the_debt(
    ledger: Ledger,
) -> None:
    invoice_debt(ledger, ("10-Mar-2026", CONTRACTOR, "650.00"))

    (debt,) = ledger.conn.execute(select(obligation)).all()
    assert (debt.status, debt.remaining) == ("paid", 0)
