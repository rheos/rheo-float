"""Derive stage 9: fiat own transfer candidate edges (FR-15, Key decision 9, edge
cases 4 and 5; AC-16 inputs).

Seam: ``derive.run`` through the derive ``ledger`` over synthetic Vancity chequing and
Vancity Visa exports, read by the real parsers with the repo's transfer wording (and
over ``FX_LINES`` documents for the currency cases), then read back as the
``own_transfer`` rows of ``pair_edge`` and the txns' ``transfer_like`` and
``hint_account_id``. Stage 9 stores candidates only (``applied`` false); stage 11
decides. Every date, amount and name is invented; 20.00, 2,000.00 and 200.00 are the
spec's own boundary figures.
"""

from __future__ import annotations

import datetime as dt
import uuid
from decimal import Decimal
from types import MappingProxyType

import pytest
from sqlalchemy import select

from rheo_finance.documents import issuers
from rheo_finance.manual.stage2 import account_id
from rheo_finance.manual.transfer_hints import TRANSFER_HINTS_FILE
from rheo_finance.parsers import registry
from rheo_finance.storage.schema import pair_edge, txn

from .support import (
    CSV_IDENTIFIER,
    FX_IDENTIFIER,
    FX_INSTITUTION,
    FX_ISSUER,
    FX_PARSER,
    Ledger,
    fx_lines,
)
from .test_conversion import cover, store_rates
from .test_reversals import OUTGOING, export

D = dt.date
SECOND = "999000000088-999-99901-99902"
THIRD = "999000000099-999-99901-99902"
FX2_IDENTIFIER = "999000000045"
CARD_NUMBER = "9999 99•• •••• 9991"

BILL_PAYMENT = "BILL PAYMENT-ONLINE VANCITY VISA 99981"
PAYMENT_RECEIVED = "PAYMENT RECEIVED -- THANK YOU"
REFUND = "SAMPLE SHOP REFUND 99975"

Row = tuple[dt.date, str, Decimal]
Edge = tuple[dt.date, Decimal, dt.date, Decimal, bool]


@pytest.fixture
def own(ledger: Ledger, monkeypatch: pytest.MonkeyPatch) -> Ledger:
    """Three chequing accounts and a Visa card at Vancity, and two accounts at the
    synthetic FX institution (``FX_LINES`` documents under ``FX_ISSUER``)."""
    monkeypatch.setattr(
        issuers,
        "ISSUER_DIRS",
        MappingProxyType(
            {**issuers.ISSUER_DIRS, FX_ISSUER: issuers.IssuerDir("statement")}
        ),
    )
    registry.load_parsers()
    monkeypatch.setitem(registry.PARSERS, FX_PARSER.parser_id, FX_PARSER)
    ledger.accounts(
        ("chequing", "Vancity", "chequing", CSV_IDENTIFIER),
        ("second", "Vancity", "chequing", SECOND),
        ("third", "Vancity", "chequing", THIRD),
        ("visa", "Vancity", "credit_card", "999000009991"),
        ("fx", FX_INSTITUTION, "chequing", FX_IDENTIFIER),
        ("fx2", FX_INSTITUTION, "chequing", FX2_IDENTIFIER),
    )
    return ledger


def on(identifier: str, *rows: Row) -> list[tuple[str, dt.date, str, Decimal]]:
    return [(identifier, day, description, amount) for day, description, amount in rows]


def visa_export(*rows: Row) -> str:
    """A Vancity Visa CSV export of ``(day, description, signed amount)`` rows, newest
    first as the bank writes it; a purchase is negative, a payment positive."""
    found = []
    for day, description, amount in sorted(rows, key=lambda row: row[0], reverse=True):
        debit = f"{-amount:.2f}" if amount < 0 else ""
        credit = f"{amount:.2f}" if amount > 0 else ""
        found.append(
            f"SAMPLE CARDHOLDER,{CARD_NUMBER},{day},{day}T00:00,{description},CAD,"
            f"{debit},{credit}"
        )
    return "\n".join(found) + "\n"


def file_bank(ledger: Ledger, *rows: tuple[str, dt.date, str, Decimal]) -> None:
    ledger.file("vancity/export.csv", export(*sorted(rows, key=lambda row: row[1])))


def file_card(ledger: Ledger, *rows: Row) -> None:
    ledger.file("vancity-visa/2025-06-30.csv", visa_export(*rows))


def edges(ledger: Ledger) -> list[Edge]:
    a, b = txn.alias("a"), txn.alias("b")
    rows = ledger.conn.execute(
        select(
            a.c.posting_date,
            a.c.amount,
            b.c.posting_date,
            b.c.amount,
            pair_edge.c.applied,
        )
        .join(a, a.c.id == pair_edge.c.a_txn_id)
        .join(b, b.c.id == pair_edge.c.b_txn_id)
        .where(pair_edge.c.kind == "own_transfer")
    )
    return sorted(tuple(row) for row in rows)


def marks(ledger: Ledger) -> list[tuple[dt.date, Decimal, bool, uuid.UUID | None]]:
    rows = ledger.conn.execute(
        select(txn.c.posting_date, txn.c.amount, txn.c.transfer_like,
               txn.c.hint_account_id)
    )  # fmt: skip
    return sorted(tuple(row) for row in rows)


def amount(value: str) -> Decimal:
    return Decimal(value)


# --- the tolerance --------------------------------------------------------------------


@pytest.mark.parametrize(
    ("paid", "received", "edge"),
    [
        ("20.00", "15.00", True),
        ("20.00", "14.99", False),
        ("2000.00", "1970.00", True),
        ("2000.00", "1969.99", False),
    ],
    ids=["20-inside", "20-outside", "2000-inside", "2000-outside"],
)
def test_the_tolerance_is_the_larger_of_five_dollars_and_one_and_a_half_percent(
    own: Ledger, paid: str, received: str, edge: bool
) -> None:
    # 20.00 tolerates 5.00 (1.5 percent is 0.30); 2,000.00 tolerates 30.00.
    sent, arrived = D(2025, 6, 2), D(2025, 6, 4)
    file_bank(own, *on(CSV_IDENTIFIER, (sent, BILL_PAYMENT, -amount(paid))))
    file_card(own, (arrived, PAYMENT_RECEIVED, amount(received)))

    own.derive()

    expected = [(sent, -amount(paid), arrived, amount(received), False)]
    assert edges(own) == (expected if edge else [])
    # Both legs are transfer-like either way: outside the tolerance they have no edge,
    # which stage 11 reads as unresolved, never ambiguous.
    assert [like for _, _, like, _ in marks(own)] == [True, True]


@pytest.mark.parametrize(("days", "edge"), [(7, True), (8, False)])
def test_the_legs_post_at_most_the_window_apart(
    own: Ledger, days: int, edge: bool
) -> None:
    sent = D(2025, 6, 2)
    arrived = sent + dt.timedelta(days=days)
    file_bank(own, *on(CSV_IDENTIFIER, (sent, BILL_PAYMENT, Decimal("-200.00"))))
    file_card(own, (arrived, PAYMENT_RECEIVED, Decimal("200.00")))

    own.derive()

    expected = [(sent, Decimal("-200.00"), arrived, Decimal("200.00"), False)]
    assert edges(own) == (expected if edge else [])


# --- only hinted lines on a card ------------------------------------------------------


@pytest.mark.parametrize("with_payment", [False, True])
def test_a_card_refund_is_never_offered_as_the_leg_of_a_chequing_debit(
    own: Ledger, with_payment: bool
) -> None:
    sent, refunded, arrived = D(2025, 6, 2), D(2025, 6, 3), D(2025, 6, 4)
    file_bank(own, *on(CSV_IDENTIFIER, (sent, BILL_PAYMENT, Decimal("-200.00"))))
    card: list[Row] = [(refunded, REFUND, Decimal("200.00"))]
    if with_payment:
        card.append((arrived, PAYMENT_RECEIVED, Decimal("200.00")))
    file_card(own, *card)

    own.derive()

    expected = [(sent, Decimal("-200.00"), arrived, Decimal("200.00"), False)]
    assert edges(own) == (expected if with_payment else [])


# --- equal card payments in one window ------------------------------------------------

TWO_BY_TWO = (
    [(D(2025, 6, 2), BILL_PAYMENT, Decimal("-200.00")),
     (D(2025, 6, 3), BILL_PAYMENT, Decimal("-200.00"))],
    [(D(2025, 6, 4), PAYMENT_RECEIVED, Decimal("200.00")),
     (D(2025, 6, 8), PAYMENT_RECEIVED, Decimal("200.00"))],
)  # fmt: skip


@pytest.mark.parametrize("card_first", [True, False], ids=["card-first", "bank-first"])
def test_two_equal_debits_and_two_equal_card_credits_thin_to_date_order(
    own: Ledger, card_first: bool
) -> None:
    debits, credits = TWO_BY_TWO
    if card_first:
        file_card(own, *credits)
        file_bank(own, *on(CSV_IDENTIFIER, *debits))
    else:
        file_bank(own, *on(CSV_IDENTIFIER, *debits))
        file_card(own, *credits)

    own.derive()

    # The k-th debit by date with the k-th credit by date, whichever was filed first.
    assert edges(own) == [
        (D(2025, 6, 2), Decimal("-200.00"), D(2025, 6, 4), Decimal("200.00"), False),
        (D(2025, 6, 3), Decimal("-200.00"), D(2025, 6, 8), Decimal("200.00"), False),
    ]


def test_three_debits_against_two_credits_keep_every_edge(own: Ledger) -> None:
    debits, credits = TWO_BY_TWO
    third = (D(2025, 6, 5), BILL_PAYMENT, Decimal("-200.00"))
    file_bank(own, *on(CSV_IDENTIFIER, *debits, third))
    file_card(own, *credits)

    own.derive()

    found = edges(own)
    assert len(found) == 6
    assert {(debit, credit) for debit, _, credit, _, _ in found} == {
        (debit, credit)
        for debit in (D(2025, 6, 2), D(2025, 6, 3), D(2025, 6, 5))
        for credit in (D(2025, 6, 4), D(2025, 6, 8))
    }


def test_unequal_amounts_keep_every_edge(own: Ledger) -> None:
    # 199.00 is inside the tolerance of 200.00, so every debit has both credits as
    # candidates, but the amounts differ: no date-order thinning.
    debits, _ = TWO_BY_TWO
    file_bank(own, *on(CSV_IDENTIFIER, *debits))
    file_card(
        own,
        (D(2025, 6, 4), PAYMENT_RECEIVED, Decimal("200.00")),
        (D(2025, 6, 8), PAYMENT_RECEIVED, Decimal("199.00")),
    )

    own.derive()

    assert len(edges(own)) == 4


def test_equal_payments_sharing_a_posting_date_keep_every_edge(own: Ledger) -> None:
    # Two debits on one day have no date order to thin by.
    day = D(2025, 6, 2)
    file_bank(
        own,
        *on(
            CSV_IDENTIFIER,
            (day, BILL_PAYMENT, Decimal("-200.00")),
            (day, BILL_PAYMENT + " 2", Decimal("-200.00")),
        ),
    )
    _, credits = TWO_BY_TWO
    file_card(own, *credits)

    own.derive()

    assert len(edges(own)) == 4


# --- the hint account: an issuer key or the owner's account slug --------------------


def test_a_pattern_hint_resolves_its_issuer_key_to_that_issuers_account(
    own: Ledger,
) -> None:
    # The bill payment's pattern names the issuer key of the card's statements, the
    # card's payment pattern the bank's; neither key is an account slug.
    sent, arrived = D(2025, 6, 2), D(2025, 6, 4)
    file_bank(own, *on(CSV_IDENTIFIER, (sent, BILL_PAYMENT, Decimal("-200.00"))))
    file_card(own, (arrived, PAYMENT_RECEIVED, Decimal("200.00")))

    own.derive()

    assert marks(own) == [
        (sent, Decimal("-200.00"), True, account_id("visa")),
        (arrived, Decimal("200.00"), True, account_id("chequing")),
    ]
    assert len(edges(own)) == 1


@pytest.mark.parametrize("counterpart", ["second", ""], ids=["named", "unnamed"])
def test_a_transfer_hints_row_names_the_owners_account_by_slug(
    own: Ledger, counterpart: str
) -> None:
    own.manual(TRANSFER_HINTS_FILE, f"sample savings sweep,out,{counterpart}")
    swept, arrived = D(2025, 6, 2), D(2025, 6, 3)
    file_bank(
        own,
        *on(CSV_IDENTIFIER, (swept, "SAMPLE SAVINGS SWEEP 99976", Decimal("-300.00"))),
        *on(SECOND, (arrived, "DEPOSIT SAMPLE SWEEP 99977", Decimal("300.00"))),
        *on(THIRD, (arrived, "DEPOSIT SAMPLE SWEEP 99978", Decimal("300.00"))),
    )

    own.derive()

    hinted = {
        row.hint_account_id
        for row in own.conn.execute(select(txn.c.hint_account_id, txn.c.amount))
        if row.amount < 0
    }
    to_second = (swept, Decimal("-300.00"), arrived, Decimal("300.00"), False)
    if counterpart:
        # Only the named account's deposit is a candidate.
        assert hinted == {account_id("second")}
        assert edges(own) == [to_second]
    else:
        # No hint account: any own account, so both deposits.
        assert hinted == {None}
        assert edges(own) == [to_second, to_second]
    # The deposits carry no wording of their own.
    assert sorted(like for _, _, like, _ in marks(own)) == [False, False, True]


def test_a_start_whose_hinted_account_has_no_candidate_searches_every_account(
    own: Ledger,
) -> None:
    # The row names ``second``; the only deposit is on ``third``. The hint is a
    # preference, so the search falls back to every own account.
    own.manual(TRANSFER_HINTS_FILE, "sample savings sweep,out,second")
    swept, arrived = D(2025, 6, 2), D(2025, 6, 3)
    file_bank(
        own,
        *on(CSV_IDENTIFIER, (swept, "SAMPLE SAVINGS SWEEP 99976", Decimal("-300.00"))),
        *on(THIRD, (arrived, "DEPOSIT SAMPLE SWEEP 99978", Decimal("300.00"))),
    )

    own.derive()

    assert edges(own) == [
        (swept, Decimal("-300.00"), arrived, Decimal("300.00"), False)
    ]
    hinted = own.conn.scalar(select(txn.c.hint_account_id).where(txn.c.amount < 0))
    assert hinted == account_id("second")


@pytest.mark.parametrize("debit_hinted", [False, True], ids=["plain", "hinted"])
def test_a_card_paid_from_a_second_banks_chequing_gets_its_edge(
    own: Ledger, debit_hinted: bool
) -> None:
    # The card's payment wording names the bank of the card's issuer, whose chequing
    # account holds lines (so the hint resolves) but no candidate. The payment came
    # from a chequing account at a second bank: the card's hint falls back to every
    # account, and the debit's own hint (when it has one) is no filter on the card.
    if debit_hinted:
        own.manual(TRANSFER_HINTS_FILE, "sample card payment,out,visa")
    paid, arrived = D(2025, 6, 3), D(2025, 6, 4)
    file_bank(own, *on(CSV_IDENTIFIER, (D(2025, 6, 1), "SAMPLE CAFE 99970",
                                        Decimal("-4.00"))))  # fmt: skip
    own.file(
        "synthetic-fx/payment.txt",
        fx_lines("CAD", "2025-06-03 -200.00 - - SAMPLE CARD PAYMENT 99971"),
    )
    file_card(own, (arrived, PAYMENT_RECEIVED, Decimal("200.00")))

    own.derive()

    assert edges(own) == [(paid, Decimal("-200.00"), arrived, Decimal("200.00"), False)]
    card_hint = own.conn.scalar(select(txn.c.hint_account_id).where(txn.c.amount > 0))
    assert card_hint == account_id("chequing")


# --- what is never a candidate --------------------------------------------------------


@pytest.mark.parametrize("reversed_", [False, True], ids=["standing", "reversed"])
def test_a_line_stage_8_reversed_is_never_a_candidate(
    own: Ledger, reversed_: bool
) -> None:
    sent, arrived, cancelled = D(2025, 6, 2), D(2025, 6, 3), D(2025, 6, 5)
    rows = [(sent, BILL_PAYMENT, Decimal("-200.00"))]
    if reversed_:
        rows.append((cancelled, OUTGOING.reversal, Decimal("200.00")))
    file_bank(own, *on(CSV_IDENTIFIER, *rows))
    file_card(own, (arrived, PAYMENT_RECEIVED, Decimal("200.00")))

    own.derive()

    standing = [(sent, Decimal("-200.00"), arrived, Decimal("200.00"), False)]
    assert edges(own) == ([] if reversed_ else standing)


# --- CAD values and unconverted legs --------------------------------------------------


@pytest.mark.parametrize(
    ("received", "edge"), [("99.00", True), ("98.00", False)], ids=["inside", "outside"]
)
def test_unconverted_legs_in_one_currency_compare_natively(
    own: Ledger, received: str, edge: bool
) -> None:
    # No USD rate is stored, so neither leg has a CAD value: the percent applies to the
    # larger native leg (1.50 of 100.00), and the CAD floor of 5.00 does not.
    own.manual(TRANSFER_HINTS_FILE, "sample move,out,")
    own.file(
        "synthetic-fx/out.txt", fx_lines("USD", "2025-12-10 -100.00 - - SAMPLE MOVE 1")
    )
    own.file(
        "synthetic-fx/in.txt",
        fx_lines(
            "USD", f"2025-12-11 {received} - - SAMPLE ARRIVAL 2", account=FX2_IDENTIFIER
        ),
    )

    own.derive()

    states = set(own.conn.scalars(select(txn.c.conversion_state)))
    assert states == {"unconverted"}
    expected = [(D(2025, 12, 10), Decimal("-100.00"), D(2025, 12, 11),
                 Decimal(received), False)]  # fmt: skip
    assert edges(own) == (expected if edge else [])


@pytest.mark.parametrize("rated", [True, False], ids=["converted", "unconverted"])
def test_legs_in_two_currencies_compare_by_cad_value_or_not_at_all(
    own: Ledger, rated: bool
) -> None:
    if rated:
        store_rates(own, "USD", (D(2025, 12, 10), "1.0200"))
        cover(own, "FXUSDCAD", (D(2025, 12, 3), D(2025, 12, 11)))
    own.manual(TRANSFER_HINTS_FILE, "sample move,out,")
    own.file(
        "synthetic-fx/out.txt", fx_lines("USD", "2025-12-10 -100.00 - - SAMPLE MOVE 1")
    )
    file_bank(
        own,
        *on(
            CSV_IDENTIFIER,
            (D(2025, 12, 11), "DEPOSIT SAMPLE ARRIVAL", Decimal("100.00")),
        ),
    )

    own.derive()

    # Converted, the USD leg is -102.00 CAD: 2.00 from the deposit, inside 5.00.
    # Unconverted, nothing measures it against a CAD line, though the two figures
    # are equal.
    expected = [(D(2025, 12, 10), Decimal("-100.00"), D(2025, 12, 11),
                 Decimal("100.00"), False)]  # fmt: skip
    assert edges(own) == (expected if rated else [])
