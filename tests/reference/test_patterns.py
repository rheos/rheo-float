"""The seam ``reference.patterns.classify`` (FR-15, FR-15(a), FR-15(c); AC-16, AC-58,
AC-85): each wording class gives its hint.

Every description is synthetic layout text in the issuers' printed forms, run through
``normalize_description`` as a parser does; every name is invented.
"""

from __future__ import annotations

from collections.abc import Sequence

import pytest

from rheo_finance.documents.issuers import ISSUER_DIRS
from rheo_finance.manual.transfer_hints import TransferHintRow
from rheo_finance.reference.patterns import (
    Hints,
    classify,
    etransfer_name,
    pattern_file,
)
from rheo_finance.text import normalize_description

BANK = ("chequing", "nelson-district-cu")
VANCITY = ("chequing", "vancity")


def hints(
    raw: str,
    account_kind: str,
    issuer: str,
    *,
    hint_rows: Sequence[TransferHintRow] = (),
    credit: bool | None = None,
) -> Hints:
    return classify(
        normalize_description(raw),
        account_kind=account_kind,
        issuer=issuer,
        hint_rows=hint_rows,
        credit=credit,
    )


# --- reversal wording, both directions


@pytest.mark.parametrize(
    "raw",
    [
        "EMT Transfer - Debit Reversal Customer Cancel",
        "Debit Reversal",
        "Customer Cancel",
    ],
)
def test_outgoing_reversal_wording_is_out(raw: str) -> None:
    assert hints(raw, *BANK).reversal_hint == "out"


@pytest.mark.parametrize(
    ("raw", "where"),
    [
        ("EMT Transfer - Credit Reversal", BANK),
        ("Item Return", BANK),
        ("Recall of deposit", BANK),
        ("NSF Return", BANK),
    ],
)
def test_incoming_reversal_wording_is_in(raw: str, where: tuple[str, str]) -> None:
    assert hints(raw, *where).reversal_hint == "in"


@pytest.mark.parametrize(
    "raw",
    [
        "E-Transfer Returned (Sample Payee)",
        "Returned Item",
        "Deposit (sample payment reversed)",
        "Sample payment Reversed",
    ],
)
def test_reversal_wording_with_no_direction_takes_the_lines_sign(raw: str) -> None:
    """'returned' and 'reversed' name no direction: a credit is the owner's own payment
    coming back (out), a debit a received payment clawed back (in)."""
    assert hints(raw, *VANCITY, credit=True).reversal_hint == "out"
    assert hints(raw, *VANCITY, credit=False).reversal_hint == "in"
    assert hints(raw, *VANCITY).reversal_hint is None


def test_the_cancelled_rent_line_is_an_outgoing_reversal_with_no_name() -> None:
    line = normalize_description("EMT Transfer - Debit Reversal Customer Cancel")
    got = classify(line, account_kind="chequing", issuer="nelson-district-cu")

    assert got == Hints(reversal_hint="out", etransfer_hint="emt transfer")
    assert etransfer_name(line, got) is None


@pytest.mark.parametrize(
    "raw", ["NSF Fee", "Sample Returns Depot", "Transfer to savings", "Recalls Ltd"]
)
def test_a_fee_or_a_word_that_only_contains_reversal_wording_is_no_reversal(
    raw: str,
) -> None:
    assert hints(raw, *BANK).reversal_hint is None


def test_reversal_wording_on_a_card_is_a_refund_not_a_reversal() -> None:
    assert hints("Return - Sample Store", "credit_card", "mbna").reversal_hint is None


# --- conversion wording


@pytest.mark.parametrize(
    ("raw", "issuer"),
    [
        ("Converted 999.00 CAD to 999.00 USD", "wise"),
        ("Moved | To CAD", "wise"),
        ("General Currency Conversion", "paypal"),
    ],
)
def test_conversion_wording_sets_the_conversion_hint(raw: str, issuer: str) -> None:
    got = hints(raw, "wallet", issuer)

    assert got.conversion_hint is True
    assert got.transfer_hint is None


def test_conversion_wording_of_another_issuer_is_no_conversion() -> None:
    got = hints("Foreign currency converted at 9.99", "credit_card", "vancity-visa")

    assert got.conversion_hint is False


# --- card payments, both sides, with their counterpart key


@pytest.mark.parametrize(
    ("raw", "issuer", "counterpart"),
    [
        ("PAYMENT RECEIVED -- THANK YOU", "vancity-visa", "vancity"),
        ("Payment Received", "mbna", None),
        ("Payment - Thank You", "mbna", None),
        ("PAYMENT", "mbna", None),
    ],
)
def test_the_card_side_payment_is_a_card_payment(
    raw: str, issuer: str, counterpart: str | None
) -> None:
    got = hints(raw, "credit_card", issuer)

    assert (got.transfer_hint, got.hint_account_slug) == ("card_payment", counterpart)


@pytest.mark.parametrize(
    ("raw", "counterpart"),
    [
        ("Bill Payment-Online (VANCITY VISA #9999)", "vancity-visa"),
        ("Bill Payment-Online (VANCITY SAMPLE VISA #9999)", "vancity-visa"),
        ("Bill Payment-Online (SAMPLE VISA #9999)", "vancity-visa"),
        ("Bill Payment-Online (MBNA SAMPLE CARD #9999)", "mbna"),
    ],
)
def test_the_chequing_side_bill_payment_names_its_card(
    raw: str, counterpart: str
) -> None:
    got = hints(raw, *VANCITY)

    assert (got.transfer_hint, got.hint_account_slug) == ("card_payment", counterpart)


def test_a_bill_payment_to_a_vendor_is_spending_not_a_transfer() -> None:
    assert hints("Bill Payment-Online (SAMPLE UTILITY #9999)", *VANCITY) == Hints()


def test_card_side_wording_on_a_bank_account_is_no_card_payment() -> None:
    assert hints("Payment Received", *VANCITY).transfer_hint is None


@pytest.mark.parametrize(
    ("raw", "issuer"),
    [("PAYMENT PLAN FEE", "mbna"), ("PAYMENT", "vancity-visa")],
)
def test_the_bare_payment_word_is_a_payment_only_on_its_own_card(
    raw: str, issuer: str
) -> None:
    assert hints(raw, "credit_card", issuer).transfer_hint is None


# --- PayPal and Stripe


@pytest.mark.parametrize(
    "raw", ["Preauthorized Payment (PAYPAL)", "Pre-Authorized Credit (PayPal)"]
)
def test_a_paypal_pull_names_paypal(raw: str) -> None:
    got = hints(raw, *VANCITY)

    assert (got.transfer_hint, got.hint_account_slug) == (
        "paypal_preauthorized",
        "paypal",
    )


@pytest.mark.parametrize(
    ("raw", "issuer", "counterpart"),
    [
        ("Received money from STRIPE", "wise", "stripe"),
        ("Payout", "stripe", "wise"),
    ],
)
def test_a_stripe_payout_names_the_other_side(
    raw: str, issuer: str, counterpart: str
) -> None:
    got = hints(raw, "payment_processor", issuer)

    assert (got.transfer_hint, got.hint_account_slug) == ("stripe_payout", counterpart)


# --- e-Transfer wording and its name position


@pytest.mark.parametrize(
    ("raw", "where", "wording", "position", "name"),
    [
        (
            "EMT Transfer - Debit Payee: Sample Landlord",
            BANK, "payee:", "after", "sample landlord",
        ),
        (
            "EMT Transfer - Credit Payer: Sample Payer",
            BANK, "payer:", "after", "sample payer",
        ),
        ("ETRANSFER CREDIT (Sample Payer)", VANCITY, "etransfer", "parenthesized",
         "sample payer"),
        ("ETRANSFER DEBIT (Sample Payee)", VANCITY, "etransfer", "parenthesized",
         "sample payee"),
        ("E-TRANSFER FROM Sample Payer", VANCITY, "e-transfer from", "after",
         "sample payer"),
        ("INTERAC E-TRANSFER Sample Payee", VANCITY, "e-transfer", "after",
         "sample payee"),
        ("INTERAC Sample Payee", VANCITY, "interac", "after", "sample payee"),
        ("TRANSFER TO Sample Payee", VANCITY, "transfer to", "after", "sample payee"),
        ("TRANSFER FROM Sample Payer", VANCITY, "transfer from", "after",
         "sample payer"),
    ],
)  # fmt: skip
def test_an_etransfer_line_has_its_wording_and_name_position(
    raw: str, where: tuple[str, str], wording: str, position: str, name: str
) -> None:
    line = normalize_description(raw)
    got = classify(line, account_kind=where[0], issuer=where[1])

    assert (got.etransfer_hint, got.etransfer_name_position) == (wording, position)
    assert etransfer_name(line, got) == name
    assert got.transfer_hint is None


def test_an_interac_card_purchase_is_no_etransfer() -> None:
    assert hints("INTERAC PURCHASE - SAMPLE STORE", *VANCITY).etransfer_hint is None


# --- fees and a plain purchase


@pytest.mark.parametrize(
    ("raw", "where", "fee_type"),
    [
        ("Service Charge", BANK, "service_charge"),
        ("NSF Fee", BANK, "nsf_fee"),
        ("Annual Fee", ("credit_card", "mbna"), "annual_fee"),
        ("Purchase Interest", ("credit_card", "vancity-visa"), "interest"),
        ("PURCHASE INT. CHARGED", ("credit_card", "vancity-visa"), "interest"),
        ("OVERLIMIT FEE", ("credit_card", "vancity-visa"), "overlimit_fee"),
        ("INTEREST CHARGE", ("credit_card", "mbna"), "interest"),
        ("Fee", ("wallet", "wise"), "fee"),
    ],
)
def test_fee_wording_names_its_fee_type(
    raw: str, where: tuple[str, str], fee_type: str
) -> None:
    assert hints(raw, *where).fee_type == fee_type


def test_a_merchant_fee_on_a_card_and_interest_on_a_bank_account_are_no_fee() -> None:
    assert hints("SAMPLE PARKING FEE", "credit_card", "vancity-visa").fee_type is None
    assert hints("Interest", *BANK).fee_type is None


@pytest.mark.parametrize(
    ("raw", "where"),
    [
        ("SAMPLE COFFEE SHOP", ("credit_card", "vancity-visa")),
        ("POS Purchase SAMPLE MARKET", BANK),
        ("Card transaction SAMPLE SHOP", ("wallet", "wise")),
    ],
)
def test_a_plain_purchase_has_no_hint(raw: str, where: tuple[str, str]) -> None:
    assert hints(raw, *where) == Hints()


# --- the owner's transfer_hints.csv rows, after the built-ins


def row(
    pattern: str, direction: str, counterpart: str | None = None
) -> TransferHintRow:
    return TransferHintRow.model_validate(
        {"pattern": pattern, "direction": direction, "counterpart_account": counterpart}
    )


def test_a_row_fills_a_transfer_hint_the_built_ins_left_empty() -> None:
    rows = [row("Sample Sweep", "out", "test-savings")]

    got = hints("SAMPLE SWEEP 9999", *VANCITY, hint_rows=rows)

    assert (got.transfer_hint, got.hint_account_slug) == ("manual_out", "test-savings")


def test_a_built_in_hint_wins_over_a_row() -> None:
    rows = [row("payment received", "in", "test-chequing")]

    got = hints("Payment Received", "credit_card", "vancity-visa", hint_rows=rows)

    assert (got.transfer_hint, got.hint_account_slug) == ("card_payment", "vancity")


def test_conversion_and_reversal_rows() -> None:
    rows = [row("sample fx", "conversion"), row("sample undo", "reversal")]

    assert hints("SAMPLE FX", "wallet", "wise", hint_rows=rows).conversion_hint
    for credit, direction in ((True, "out"), (False, "in")):
        got = hints("SAMPLE UNDO", *BANK, hint_rows=rows, credit=credit)
        assert got.reversal_hint == direction
    assert hints("SAMPLE UNDO", *BANK, hint_rows=rows).reversal_hint is None


def test_a_row_matches_whole_tokens_only() -> None:
    rows = [row("sweep", "out")]

    assert hints("SAMPLE SWEEPSTAKES", *VANCITY, hint_rows=rows) == Hints()


def test_a_word_character_right_before_a_wording_is_no_match() -> None:
    """The leading boundary: a wording never matches from inside a word."""
    rows = [row("sweep", "out")]

    assert hints("AUTOSWEEP 9999", *VANCITY, hint_rows=rows) == Hints()
    assert hints("SAMPLE SWEEP 9999", *VANCITY, hint_rows=rows).transfer_hint == (
        "manual_out"
    )
    assert hints("PREPAYMENT RECEIVED", "credit_card", "mbna") == Hints()


# --- the file itself


def test_every_issuer_and_counterpart_in_the_file_is_an_issuer_key() -> None:
    patterns = pattern_file()
    named = {p.counterpart for p in patterns.transfer if p.counterpart}
    for section in (
        patterns.transfer,
        patterns.reversal,
        patterns.conversion,
        patterns.fee,
        patterns.etransfer,
    ):
        for entry in section:
            named.update(entry.issuers or ())
    assert named
    assert named <= set(ISSUER_DIRS)
