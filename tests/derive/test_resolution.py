"""Derive stage 11 for fiat: mutual uniqueness, the transfer states and the derived fee
lines (FR-15, FR-16, Key decision 9, edge cases 2, 3, 5 and 70; AC-16, AC-17, AC-18
and the AC-80 pairing clause).

Seam: ``derive.run`` through the derive ``ledger`` over synthetic exports read by the
real parsers (Vancity chequing and Visa, the PayPal activity CSV, the Wise statement
CSV, the Stripe balance CSV) and a manual cash line, then read back as the txn states,
the ``own_transfer`` edges with their ``applied`` flag and fee line, and spending and
income as ``ledger.totals.cad_total`` sums the lines that count: negative and positive
lines with no transfer state (SPEC "Derivation", stage 13's spending rule). Every
date, amount and name is invented.
"""

from __future__ import annotations

import csv
import datetime as dt
from collections.abc import Callable
from decimal import Decimal

import pytest
from sqlalchemy import ColumnElement, select

from rheo_finance.ledger.totals import cad_total
from rheo_finance.manual.accounts import ACCOUNTS_FILE, IDENTIFIERS_FILE
from rheo_finance.manual.stage2 import account_id
from rheo_finance.manual.transfer_hints import TRANSFER_HINTS_FILE
from rheo_finance.storage.schema import pair_edge, txn, txn_source

from .support import CSV_IDENTIFIER, FIXTURES, Ledger
from .test_reversals import OUTGOING, export
from .test_transfer_edges import BILL_PAYMENT, PAYMENT_RECEIVED, SECOND, visa_export

D = dt.date
CARD_ACCOUNT = "999000009991"
PAYPAL_ID = "SAMPLE999A9ID"
WISE_ID = "P99900001"
STRIPE_ID = "SAMPLE999STRIPE"
EXCHANGE_ID = "999000000055"

PAYPAL_PULL = "PREAUTHORIZED DEBIT PAYPAL 99982"
EXCHANGE_OUT = "SAMPLE EXCHANGE DEPOSIT 99983"
WISE_IN = "DEPOSIT SAMPLE ARRIVAL 99984"
WISE_OUT = "Sent money to Sample Account Holder"
HINT_ROWS = (
    "sample exchange deposit,out,exchange",
    "sent money to sample account holder,out,chequing",
)

State = tuple[dt.date, Decimal, str | None, str | None]
Account = tuple[str, str, str, str, str]


def register(ledger: Ledger, *rows: Account) -> None:
    """Accounts as ``(slug, institution, kind, currencies, identifier)``."""
    ledger.manual(
        ACCOUNTS_FILE,
        *(f"{slug},{slug.title()},{inst},{kind},{currencies},monthly,,,,,,,,,"
          for slug, inst, kind, currencies, _ in rows),
    )  # fmt: skip
    ledger.manual(
        IDENTIFIERS_FILE, *(f"{slug},{ident},number,," for slug, *_, ident in rows)
    )


CHEQUING: Account = ("chequing", "Vancity", "chequing", "CAD", CSV_IDENTIFIER)
SECOND_CHEQUING: Account = ("second", "Vancity", "chequing", "CAD", SECOND)
VISA: Account = ("visa", "Vancity", "credit_card", "CAD", CARD_ACCOUNT)
PAYPAL: Account = ("paypal", "PayPal", "payment_processor", "CAD;USD", PAYPAL_ID)
WISE: Account = ("wise", "Wise", "wallet", "CAD;USD", WISE_ID)
STRIPE: Account = ("stripe", "Stripe", "payment_processor", "CAD;USD", STRIPE_ID)
EXCHANGE: Account = ("exchange", "Sample Exchange", "exchange", "CAD", EXCHANGE_ID)


def header(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8").splitlines()[0]


def table(name: str, rows: list[dict[str, str]]) -> str:
    """A CSV with the fixture ``name``'s header and ``rows`` (absent cells empty)."""
    top = header(name)
    columns = next(csv.reader([top]))
    body = [",".join(f'"{row.get(c, "")}"' for c in columns) for row in rows]
    return "\n".join([top, *body]) + "\n"


def paypal_csv(*rows: tuple[dt.date, str, str, Decimal]) -> str:
    """A PayPal activity CSV in CAD of ``(day, type, name, gross)`` rows, oldest
    first, no fees, the balance running from zero."""
    balance = Decimal(0)
    found = []
    for n, (day, kind, name, gross) in enumerate(rows):
        balance += gross
        found.append(
            {
                "Date": day.strftime("%d/%m/%Y"), "Time": f"08:00:{n:02d}",
                "TimeZone": "PDT", "Name": name, "Type": kind, "Status": "Completed",
                "Currency": "CAD", "Gross": f"{gross:.2f}", "Fee": "0.00",
                "Net": f"{gross:.2f}", "Balance": f"{balance:.2f}",
                "Balance Impact": "Credit" if gross > 0 else "Debit",
            }
        )  # fmt: skip
    return table("paypal_activity.csv", found)


def wise_csv(*rows: tuple[dt.date, str, Decimal, Decimal]) -> str:
    """A Wise statement CSV in CAD of ``(day, description, amount with fee, fee)``
    rows, given oldest first and written newest first as the export is, the balance
    running from 2,000.00."""
    balance = Decimal("2000.00")
    found = []
    for n, (day, description, amount, fee) in enumerate(rows):
        balance += amount
        found.append(
            {
                "TransferWise ID": f"TRANSFER-99900010{n}",
                "Date": day.strftime("%d-%m-%Y"), "Amount": f"{amount:.2f}",
                "Currency": "CAD", "Description": description,
                "Running Balance": f"{balance:.2f}", "Total fees": f"{fee:.2f}",
            }
        )  # fmt: skip
    return table("wise_statement.csv", found[::-1])


def states(ledger: Ledger) -> list[State]:
    rows = ledger.conn.execute(
        select(txn.c.posting_date, txn.c.amount, txn.c.transfer_state, txn.c.pair_kind)
    )
    return sorted(tuple(row) for row in rows)


def applied(ledger: Ledger) -> list[bool]:
    return sorted(
        ledger.conn.scalars(
            select(pair_edge.c.applied).where(pair_edge.c.kind == "own_transfer")
        )
    )


def total(ledger: Ledger, *filters: ColumnElement[bool]) -> Decimal:
    """The CAD sum of the lines that count (no transfer state) ``filters`` select."""
    found = cad_total(ledger.uow, [txn.c.transfer_state.is_(None), *filters])
    assert found.unconverted_count == 0
    return found.sum


def spending(ledger: Ledger, *filters: ColumnElement[bool]) -> Decimal:
    return total(ledger, txn.c.amount < 0, *filters)


def income(ledger: Ledger, *filters: ColumnElement[bool]) -> Decimal:
    return total(ledger, txn.c.amount > 0, *filters)


def transfer_like_states(ledger: Ledger) -> list[str | None]:
    return list(
        ledger.conn.scalars(select(txn.c.transfer_state).where(txn.c.transfer_like))
    )


# --- AC-16: four movements between own accounts, each counted zero times --------------


def four_movements(ledger: Ledger) -> list[Callable[[], object]]:
    """A card payment, a PayPal payment funded by the bank, an exchange deposit and a
    Wise to chequing transfer whose chequing leg arrived 5.00 short, as the steps
    that file them."""
    register(ledger, CHEQUING, VISA, PAYPAL, WISE, EXCHANGE)
    ledger.manual(TRANSFER_HINTS_FILE, *HINT_ROWS)
    ledger.cash(f"exchange,{D(2025, 6, 10)},500.00,CAD,SAMPLE EXCHANGE CREDIT,")
    bank = export(
        (CSV_IDENTIFIER, D(2025, 6, 2), BILL_PAYMENT, Decimal("-200.00")),
        (CSV_IDENTIFIER, D(2025, 6, 5), PAYPAL_PULL, Decimal("-40.00")),
        (CSV_IDENTIFIER, D(2025, 6, 9), EXCHANGE_OUT, Decimal("-500.00")),
        (CSV_IDENTIFIER, D(2025, 6, 12), WISE_IN, Decimal("1000.00")),
    )
    card = visa_export(
        (D(2025, 5, 28), "SAMPLE SHOP 99985", Decimal("-200.00")),
        (D(2025, 6, 4), PAYMENT_RECEIVED, Decimal("200.00")),
    )
    paypal = paypal_csv(
        (D(2025, 6, 6), "Bank Deposit to PP Account", "", Decimal("40.00")),
        (D(2025, 6, 6), "Express Checkout Payment", "Sample Merchant",
         Decimal("-40.00")),
    )  # fmt: skip
    wise = wise_csv((D(2025, 6, 11), WISE_OUT, Decimal("-1005.00"), Decimal("0")))
    return [
        lambda: ledger.file("vancity/export.csv", bank),
        lambda: ledger.file("vancity-visa/2025-06-30.csv", card),
        lambda: ledger.file("paypal/activity-2025-06-30.csv", paypal),
        lambda: ledger.file("wise/statement-2025-06-30.csv", wise),
    ]


PAIRED = ("paired", "own_transfer")
AC16_STATES: list[State] = [
    (D(2025, 5, 28), Decimal("-200.00"), None, None),  # the card purchase
    (D(2025, 6, 2), Decimal("-200.00"), *PAIRED),
    (D(2025, 6, 4), Decimal("200.00"), *PAIRED),
    (D(2025, 6, 5), Decimal("-40.00"), *PAIRED),
    (D(2025, 6, 6), Decimal("-40.00"), None, None),  # the PayPal purchase
    (D(2025, 6, 6), Decimal("40.00"), *PAIRED),
    (D(2025, 6, 9), Decimal("-500.00"), *PAIRED),
    (D(2025, 6, 10), Decimal("500.00"), *PAIRED),
    (D(2025, 6, 11), Decimal("-1005.00"), *PAIRED),
    (D(2025, 6, 11), Decimal("-5.00"), None, None),  # the derived fee
    (D(2025, 6, 12), Decimal("1000.00"), *PAIRED),
]


@pytest.mark.parametrize("order", ["filed", "reversed"])
def test_movements_between_own_accounts_count_zero_times_and_the_fee_once(
    ledger: Ledger, order: str
) -> None:
    steps = four_movements(ledger)
    for step in steps if order == "filed" else steps[::-1]:
        step()

    ledger.derive()

    # The same result whichever order the documents were imported in.
    assert states(ledger) == AC16_STATES
    assert applied(ledger) == [True] * 4
    # Spending is the two purchases and the fee; nothing is income.
    assert spending(ledger) == Decimal("-245.00")
    assert income(ledger) == Decimal("0.00")


def test_the_derived_fee_is_one_line_on_the_outbound_account(ledger: Ledger) -> None:
    for step in four_movements(ledger):
        step()

    ledger.derive()

    fees = ledger.conn.execute(
        select(txn.c.id, txn.c.account_id, txn.c.posting_date, txn.c.amount,
               txn.c.currency, txn.c.cad_amount, txn.c.tax_year, txn.c.fee_type)
        .where(txn.c.origin == "derived_fee")
    ).all()  # fmt: skip
    assert [tuple(fee)[1:] for fee in fees] == [
        (account_id("wise"), D(2025, 6, 11), Decimal("-5.00"), "CAD",
         Decimal("-5.00"), 2025, "fee"),
    ]  # fmt: skip
    # The Wise transfer's edge names it; the other three balance and name none.
    named = ledger.conn.scalars(
        select(pair_edge.c.fee_txn_id).where(pair_edge.c.fee_txn_id.is_not(None))
    ).all()
    assert named == [fees[0].id]
    source = ledger.conn.execute(
        select(txn_source.c.role, txn_source.c.line_key_id).where(
            txn_source.c.txn_id == fees[0].id
        )
    ).all()
    assert [tuple(row) for row in source] == [("primary", fees[0].id)]


@pytest.mark.parametrize("printed", [True, False], ids=["printed", "derived"])
def test_a_transfer_fee_counts_once_printed_or_derived(
    ledger: Ledger, printed: bool
) -> None:
    # Printed, the export's fee is its own line and the legs balance after it; not
    # printed, the legs differ by it and stage 11 books it.
    register(ledger, CHEQUING, WISE)
    ledger.manual(TRANSFER_HINTS_FILE, *HINT_ROWS)
    fee = Decimal("5.00") if printed else Decimal("0")
    ledger.file(
        "wise/statement-2025-06-30.csv",
        wise_csv((D(2025, 6, 11), WISE_OUT, Decimal("-1005.00"), fee)),
    )
    ledger.file(
        "vancity/export.csv",
        export((CSV_IDENTIFIER, D(2025, 6, 12), WISE_IN, Decimal("1000.00"))),
    )

    ledger.derive()

    fee_lines = ledger.conn.execute(
        select(txn.c.origin, txn.c.amount).where(txn.c.fee_type.is_not(None))
    ).all()
    origin = "document_line" if printed else "derived_fee"
    assert [tuple(row) for row in fee_lines] == [(origin, Decimal("-5.00"))]
    assert applied(ledger) == [True]
    assert spending(ledger) == Decimal("-5.00")
    assert income(ledger) == Decimal("0.00")


# --- AC-16, AC-17: ambiguous and unresolved -------------------------------------------


def test_two_candidate_counterparts_leave_every_line_ambiguous(ledger: Ledger) -> None:
    register(ledger, CHEQUING, VISA)
    ledger.file(
        "vancity/export.csv",
        export((CSV_IDENTIFIER, D(2025, 6, 2), BILL_PAYMENT, Decimal("-200.00"))),
    )
    ledger.file(
        "vancity-visa/2025-06-30.csv",
        visa_export(
            (D(2025, 6, 4), PAYMENT_RECEIVED, Decimal("200.00")),
            (D(2025, 6, 6), PAYMENT_RECEIVED, Decimal("200.00")),
        ),
    )

    ledger.derive()

    assert states(ledger) == [
        (D(2025, 6, 2), Decimal("-200.00"), "ambiguous", None),
        (D(2025, 6, 4), Decimal("200.00"), "ambiguous", None),
        (D(2025, 6, 6), Decimal("200.00"), "ambiguous", None),
    ]
    assert applied(ledger) == [False, False]
    assert income(ledger) == spending(ledger) == Decimal("0.00")


def test_two_debits_sharing_their_one_credit_leave_all_three_ambiguous(
    ledger: Ledger,
) -> None:
    register(ledger, CHEQUING, SECOND_CHEQUING, VISA)
    ledger.file(
        "vancity/export.csv",
        export(
            (CSV_IDENTIFIER, D(2025, 6, 2), BILL_PAYMENT, Decimal("-200.00")),
            (SECOND, D(2025, 6, 3), BILL_PAYMENT, Decimal("-200.00")),
        ),
    )
    ledger.file(
        "vancity-visa/2025-06-30.csv",
        visa_export((D(2025, 6, 4), PAYMENT_RECEIVED, Decimal("200.00"))),
    )

    ledger.derive()

    assert states(ledger) == [
        (D(2025, 6, 2), Decimal("-200.00"), "ambiguous", None),
        (D(2025, 6, 3), Decimal("-200.00"), "ambiguous", None),
        (D(2025, 6, 4), Decimal("200.00"), "ambiguous", None),
    ]
    assert applied(ledger) == [False, False]


@pytest.mark.parametrize(
    ("received", "state"),
    [("15.00", "paired"), ("14.99", "unresolved")],
    ids=["inside", "outside"],
)
def test_a_lone_counterpart_outside_the_tolerance_leaves_both_unresolved(
    ledger: Ledger, received: str, state: str
) -> None:
    register(ledger, CHEQUING, VISA)
    ledger.file(
        "vancity/export.csv",
        export((CSV_IDENTIFIER, D(2025, 6, 2), BILL_PAYMENT, Decimal("-20.00"))),
    )
    ledger.file(
        "vancity-visa/2025-06-30.csv",
        visa_export((D(2025, 6, 4), PAYMENT_RECEIVED, Decimal(received))),
    )

    ledger.derive()

    assert sorted(transfer_like_states(ledger), key=str) == [state, state]


# --- AC-18: a card payment across a year end ------------------------------------------


def test_a_december_purchase_paid_in_january_counts_once_in_the_earlier_year(
    ledger: Ledger,
) -> None:
    register(ledger, CHEQUING, VISA)
    ledger.file(
        "vancity/export.csv",
        export((CSV_IDENTIFIER, D(2024, 12, 30), BILL_PAYMENT, Decimal("-300.00"))),
    )
    ledger.file(
        "vancity-visa/2025-01-31.csv",
        visa_export(
            (D(2024, 12, 30), "SAMPLE SHOP 99986", Decimal("-300.00")),
            (D(2025, 1, 2), PAYMENT_RECEIVED, Decimal("300.00")),
        ),
    )

    ledger.derive()

    purchase_year = ledger.conn.scalar(
        select(txn.c.tax_year).where(txn.c.transfer_state.is_(None))
    )
    assert purchase_year == 2024
    assert transfer_like_states(ledger) == ["paired", "paired"]
    assert spending(ledger, txn.c.tax_year == 2024) == Decimal("-300.00")
    assert spending(ledger, txn.c.tax_year == 2025) == Decimal("0.00")


# --- AC-80: a Wise deposit from Stripe and the Stripe payout --------------------------


@pytest.mark.parametrize("with_stripe", [True, False], ids=["stripe", "no-stripe"])
def test_a_wise_deposit_from_stripe_pairs_with_the_payout_or_ends_unresolved(
    ledger: Ledger, with_stripe: bool
) -> None:
    register(ledger, WISE, STRIPE)
    wise = (FIXTURES / "wise_statement.csv").read_text(encoding="utf-8")
    ledger.file("wise/statement-2026-02-28.csv", wise)
    if with_stripe:
        stripe = (FIXTURES / "stripe_balance.csv").read_text(encoding="utf-8")
        ledger.file("stripe/balance-2026-02-26.csv", stripe)

    ledger.derive()

    found = states(ledger)
    deposit = (D(2026, 2, 27), Decimal("250.00"))
    payout = (D(2026, 2, 25), Decimal("-250.00"))
    if with_stripe:
        assert (*deposit, *PAIRED) in found
        assert (*payout, *PAIRED) in found
        assert applied(ledger) == [True]
    else:
        assert (*deposit, "unresolved", None) in found
        assert applied(ledger) == []
    # AC-17: every transfer-like line is in exactly one of the three states.
    assert set(transfer_like_states(ledger)) <= {"paired", "ambiguous", "unresolved"}
    assert None not in transfer_like_states(ledger)


# --- stage 8's reversals stay as stage 8 settled them ---------------------------------


def test_a_reversed_payment_stays_settled_and_its_card_leg_is_unresolved(
    ledger: Ledger,
) -> None:
    register(ledger, CHEQUING, VISA)
    sent, arrived, cancelled = D(2025, 6, 2), D(2025, 6, 4), D(2025, 6, 5)
    ledger.file(
        "vancity/export.csv",
        export(
            (CSV_IDENTIFIER, sent, BILL_PAYMENT, Decimal("-200.00")),
            (CSV_IDENTIFIER, cancelled, OUTGOING.reversal, Decimal("200.00")),
        ),
    )
    ledger.file(
        "vancity-visa/2025-06-30.csv",
        visa_export((arrived, PAYMENT_RECEIVED, Decimal("200.00"))),
    )

    ledger.derive()

    assert states(ledger) == [
        (sent, Decimal("-200.00"), "paired", "reversal"),
        (arrived, Decimal("200.00"), "unresolved", None),
        (cancelled, Decimal("200.00"), "paired", "reversal"),
    ]
    reversal = ledger.conn.scalars(
        select(pair_edge.c.applied).where(pair_edge.c.kind == "reversal")
    ).all()
    assert reversal == [True]


def test_the_original_of_a_same_day_reversal_is_not_made_ambiguous(
    ledger: Ledger,
) -> None:
    # Stage 8 offers the same-day original as the reversal's one candidate (the
    # reversal ``ambiguous``) and leaves the original an ordinary line. That edge is
    # stage 8's: the original, a transfer-like payment with no transfer candidate,
    # is unresolved, never ambiguous on its account.
    register(ledger, CHEQUING)
    day = D(2025, 6, 2)
    ledger.file(
        "vancity/export.csv",
        export(
            (CSV_IDENTIFIER, day, BILL_PAYMENT, Decimal("-200.00")),
            (CSV_IDENTIFIER, day, OUTGOING.reversal, Decimal("200.00")),
        ),
    )

    ledger.derive()

    assert states(ledger) == [
        (day, Decimal("-200.00"), "unresolved", None),
        (day, Decimal("200.00"), "ambiguous", None),
    ]
    reversal = ledger.conn.scalars(
        select(pair_edge.c.applied).where(pair_edge.c.kind == "reversal")
    ).all()
    assert reversal == [False]


def test_a_same_day_reversal_edge_and_a_card_candidate_leave_both_ambiguous(
    ledger: Ledger,
) -> None:
    # Uniqueness counts every kind together: the payment is the original a same-day
    # reversal names (stage 8's edge, not applied) and the card credit's one transfer
    # candidate. Two edges on the payment, so the transfer edge does not apply.
    register(ledger, CHEQUING, VISA)
    day, arrived = D(2025, 6, 2), D(2025, 6, 4)
    ledger.file(
        "vancity/export.csv",
        export(
            (CSV_IDENTIFIER, day, BILL_PAYMENT, Decimal("-200.00")),
            (CSV_IDENTIFIER, day, OUTGOING.reversal, Decimal("200.00")),
        ),
    )
    ledger.file(
        "vancity-visa/2025-06-30.csv",
        visa_export((arrived, PAYMENT_RECEIVED, Decimal("200.00"))),
    )

    ledger.derive()

    assert states(ledger) == [
        (day, Decimal("-200.00"), "ambiguous", None),
        (day, Decimal("200.00"), "ambiguous", None),
        (arrived, Decimal("200.00"), "ambiguous", None),
    ]
    assert applied(ledger) == [False]
    assert not ledger.conn.scalar(select(pair_edge.c.applied).where(
        pair_edge.c.kind == "reversal"))  # fmt: skip
    assert income(ledger) == spending(ledger) == Decimal("0.00")
