"""Receipts and invoices match by the payees' patterns (FR-8, FR-10; Derivation stage 3
"Receipts and invoices").

Seam: ``derive.run`` through the derive ``ledger`` over a synthetic chequing export and
the synthetic patient receipt or invoice, as in ``test_receipts_invoices``, with a
``payees.csv`` or ``accounts.csv`` ``payee_patterns`` that registers one payee under
two printed names: the receipt's (or invoice's) and the payment line's. Every name,
date and amount is invented.
"""

from __future__ import annotations

from decimal import Decimal

from rheo_finance.manual.accounts import ACCOUNTS_FILE
from rheo_finance.manual.payees import PAYEES_FILE

from .support import Ledger
from .test_receipts_invoices import (
    INVOICE,
    PAID_OUTSIDE,
    RECEIPT,
    counts,
    debit_only_receipt,
    invoice,
    payments,
    sources,
    spending,
    txn_of,
)

# The card terminal prints a short name the receipt never does.
TERMINAL = "POS PURCHASE   SDC SAMPLETOWN   99907"
PAID_TO_FIRM = "E-TRANSFER SENT   SC FIRM LTD   99908"


def test_a_receipt_matches_a_payment_only_by_a_payees_pattern(ledger: Ledger) -> None:
    payments(ledger, ("15-Mar-2026", TERMINAL, "45.00"))
    ledger.file(RECEIPT, debit_only_receipt())

    unregistered = ledger.derive()
    assert counts(unregistered) == (2, 0, 1, 0)

    ledger.manual(
        PAYEES_FILE, "dentist,Dentist,vendor,Sample Dental Clinic;SDC SAMPLETOWN,,"
    )
    registered = ledger.derive()

    assert counts(registered) == (2, 1, 0, 0)
    (link,) = sources(ledger, "evidence")
    assert (link.txn_id, link.line_kind) == (txn_of(ledger, TERMINAL), "receipt")
    assert [r for r in ledger.txns() if r["account_id"] == PAID_OUTSIDE] == []
    assert spending(ledger) == Decimal("-45.00")


def test_a_pattern_names_the_receipt_as_whole_words_only(ledger: Ledger) -> None:
    """A pattern that is only part of a word of the receipt's payee does not make the
    payee its own."""
    payments(ledger, ("15-Mar-2026", TERMINAL, "45.00"))
    ledger.file(RECEIPT, debit_only_receipt())
    ledger.manual(PAYEES_FILE, "dentist,Dentist,vendor,Sample Dent;SDC SAMPLETOWN,,")

    assert counts(ledger.derive()) == (2, 0, 1, 0)


def test_an_accounts_payee_patterns_match_as_a_payees_row_does(
    ledger: Ledger,
) -> None:
    """The invoice prints the contractor's name; the payment line prints the firm's.
    One account-backed payee holding both makes the payment the invoice's."""
    payments(ledger, ("31-Dec-2025", PAID_TO_FIRM, "750.00"))
    ledger.file(INVOICE, invoice())
    assert counts(ledger.derive()) == (2, 0, 0, 1)

    accounts = (ledger.data / "manual" / ACCOUNTS_FILE).read_text(encoding="utf-8")
    _, row = accounts.splitlines()
    cells = row.split(",")
    cells[11] = "Sample Contractor;SC FIRM LTD"
    ledger.manual(ACCOUNTS_FILE, ",".join(cells))
    second = ledger.derive()

    assert counts(second) == (2, 1, 0, 0)
    (link,) = sources(ledger, "evidence")
    assert (link.txn_id, link.line_kind) == (txn_of(ledger, PAID_TO_FIRM), "invoice")
