"""Receipts and invoices in derive stage 3 (FR-8, FR-16; AC-8 matching clauses).

Seam: ``derive.run`` over a synthetic chequing export (the payment lines) beside the
synthetic patient receipt or invoice, read back through ``txn`` and ``txn_source``. A
receipt or invoice line that matches a payment line (payee, amount, date window) is an
``evidence`` source of that payment and adds no txn; an unmatched receipt is one txn
on "Paid outside recorded accounts" at its date; an unmatched invoice, however old,
adds no txn and nothing to any spending total. Every value is synthetic.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import func, select

from rheo_finance.derive.run import Derived
from rheo_finance.manual.stage2 import account_id
from rheo_finance.storage.schema import document_line, txn, txn_source

from .support import FIXTURES, D, Ledger, csv_row

RECEIPT = "receipts/sample-clinic/2026-03-16.pdf"
INVOICE = "invoices-received/sample-vendor/2025-12-01.pdf"
PAID_OUTSIDE = account_id("paid-outside")
CLINIC = "POS PURCHASE   SAMPLE DENTAL CLINIC   99907"
CONTRACTOR = "E-TRANSFER SENT   SAMPLE CONTRACTOR   99908"
OTHER_SHOP = "POS PURCHASE   SAMPLE HARDWARE   99904"
CASH_ROW = "14-Mar-26   Cash   Pmt. Payment #999123 - Patient, Sample   $200.00\n"


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def changed(text: str, printed: str, replacement: str) -> str:
    assert text.count(printed) == 1, printed
    return text.replace(printed, replacement)


def debit_only_receipt() -> str:
    """The receipt with its 45.00 debit payment of 15 March alone."""
    text = changed(fixture("patient_receipt.txt"), CASH_ROW, "")
    return changed(text, "Total Payments   $245.00", "Total Payments   $45.00")


def invoice(issued: str = "Mon, Dec 1, 2025") -> str:
    return changed(fixture("invoice_received.txt"), "Mon, Dec 1, 2025", issued)


def payments(ledger: Ledger, *rows: tuple[str, str, str]) -> None:
    """A chequing export of ``(day, description, amount out)`` rows, newest first,
    above an opening deposit of 1000.00 on 1 Jan 2019."""
    balance = Decimal("1000.00") - sum((Decimal(out) for _, _, out in rows), Decimal(0))
    lines = []
    for day, description, out in rows:
        lines.append(csv_row(day, description, out=out, balance=f"{balance:.2f}"))
        balance += Decimal(out)
    lines.append(
        csv_row("01-Jan-2019", "DEPOSIT   SAMPLE PAYROLL   99906", into="1000.00",
                balance="1000.00")
    )  # fmt: skip
    ledger.chequing()
    ledger.file("vancity/export.csv", "\n".join(lines) + "\n")


def spending(ledger: Ledger) -> Decimal:
    """Every outflow in the ledger: the sum of the negative txns."""
    total = ledger.conn.scalar(select(func.sum(txn.c.amount)).where(txn.c.amount < 0))
    return Decimal(total or 0)


def sources(ledger: Ledger, role: str) -> list[Any]:
    return list(
        ledger.conn.execute(
            select(txn_source, document_line.c.line_kind, document_line.c.amount)
            .join(document_line, document_line.c.id == txn_source.c.document_line_id)
            .where(txn_source.c.role == role)
            .order_by(document_line.c.posting_date)
        ).all()
    )


def txn_of(ledger: Ledger, description: str) -> uuid.UUID:
    norm = " ".join(description.casefold().split())
    (found,) = [row["id"] for row in ledger.txns() if row["description_norm"] == norm]
    assert isinstance(found, uuid.UUID)
    return found


def counts(out: Derived) -> tuple[int, int, int, int]:
    lines = out.lines
    return (
        lines.from_documents, lines.evidence_links, lines.from_receipts,
        lines.unmatched_invoices,
    )  # fmt: skip


# --- receipts -------------------------------------------------------------------------


def test_a_matched_receipt_is_evidence_and_adds_no_txn(ledger: Ledger) -> None:
    payments(ledger, ("15-Mar-2026", CLINIC, "45.00"))
    ledger.file(RECEIPT, debit_only_receipt())

    out = ledger.derive()

    assert counts(out) == (2, 1, 0, 0)
    assert len(ledger.txns()) == 2
    assert spending(ledger) == Decimal("-45.00")
    (link,) = sources(ledger, "evidence")
    assert (link.txn_id, link.line_kind, link.amount) == (
        txn_of(ledger, CLINIC), "receipt", Decimal("-45.00"),
    )  # fmt: skip
    assert [r for r in ledger.txns() if r["account_id"] == PAID_OUTSIDE] == []


def test_an_unmatched_receipt_is_one_txn_on_paid_outside(ledger: Ledger) -> None:
    """The cash payment of 14 March has no payment line: it is its own txn on the
    internal account at its date. The debit of 15 March matches the card line and
    counts once."""
    payments(ledger, ("15-Mar-2026", CLINIC, "45.00"))
    ledger.file(RECEIPT, fixture("patient_receipt.txt"))

    out = ledger.derive()

    assert counts(out) == (2, 1, 1, 0)
    assert out.lines.txns == 3
    (outside,) = [r for r in ledger.txns() if r["account_id"] == PAID_OUTSIDE]
    assert (outside["posting_date"], outside["amount"], outside["currency"]) == (
        D(2026, 3, 14), Decimal("-200.00"), "CAD",
    )  # fmt: skip
    assert (outside["origin"], outside["counterparty"]) == (
        "document_expense", "sample dental clinic",
    )  # fmt: skip
    (primary,) = [
        row for row in sources(ledger, "primary") if row.txn_id == outside["id"]
    ]
    assert (primary.line_kind, primary.line_key_id) == ("receipt", outside["id"])
    assert spending(ledger) == Decimal("-245.00")


@pytest.mark.parametrize(
    ("day", "matched"),
    [("18-Mar-2026", True), ("19-Mar-2026", False), ("11-Mar-2026", False)],
    ids=["three_days_after", "four_days_after", "four_days_before"],
)
def test_a_receipt_matches_inside_its_window_only(
    ledger: Ledger, day: str, matched: bool
) -> None:
    payments(ledger, (day, CLINIC, "45.00"))
    ledger.file(RECEIPT, debit_only_receipt())

    out = ledger.derive()

    assert (out.lines.evidence_links, out.lines.from_receipts) == (
        (1, 0) if matched else (0, 1)
    )
    assert spending(ledger) == Decimal("-45.00") * (1 if matched else 2)


def test_a_receipt_needs_its_payee_on_the_payment_line(ledger: Ledger) -> None:
    payments(ledger, ("15-Mar-2026", OTHER_SHOP, "45.00"))
    ledger.file(RECEIPT, debit_only_receipt())

    out = ledger.derive()

    assert counts(out) == (2, 0, 1, 0)
    assert spending(ledger) == Decimal("-90.00")


def test_a_receipt_needs_the_same_amount(ledger: Ledger) -> None:
    payments(ledger, ("15-Mar-2026", CLINIC, "45.01"))
    ledger.file(RECEIPT, debit_only_receipt())

    assert counts(ledger.derive()) == (2, 0, 1, 0)


def test_two_receipt_payments_never_share_one_payment_line(ledger: Ledger) -> None:
    """Two debits of 45.00 a day apart and one card line of 45.00 on the second day:
    the exact date takes the line, and the other debit is paid outside."""
    text = changed(
        fixture("patient_receipt.txt"),
        CASH_ROW,
        CASH_ROW.replace("Cash", "Debit").replace("$200.00", "$45.00"),
    )
    payments(ledger, ("15-Mar-2026", CLINIC, "45.00"))
    ledger.file(RECEIPT, changed(text, "$245.00", "$90.00"))

    out = ledger.derive()

    assert counts(out) == (2, 1, 1, 0)
    (outside,) = [r for r in ledger.txns() if r["account_id"] == PAID_OUTSIDE]
    assert outside["posting_date"] == D(2026, 3, 14)
    assert spending(ledger) == Decimal("-90.00")


def test_a_second_copy_of_a_receipt_adds_nothing(ledger: Ledger) -> None:
    """A re-saved copy (other bytes, the same lines) is the same receipt: its lines
    are evidence of the txns the first copy reached, and nothing counts twice."""
    payments(ledger, ("15-Mar-2026", CLINIC, "45.00"))
    text = fixture("patient_receipt.txt")
    ledger.file(RECEIPT, text)
    ledger.file(
        "receipts/sample-clinic/2026-03-16-copy.pdf", text.replace("\n", "\r\n")
    )

    out = ledger.derive()

    assert out.documents == {"parsed": 3}
    assert counts(out) == (2, 3, 1, 0)
    assert out.lines.txns == 3
    assert spending(ledger) == Decimal("-245.00")


# --- invoices -------------------------------------------------------------------------


@pytest.mark.parametrize("issued", ["Mon, Dec 1, 2025", "Sun, Dec 1, 2019"])
def test_an_unmatched_invoice_adds_no_txn_and_no_spending(
    ledger: Ledger, issued: str
) -> None:
    """However old, an invoice no payment line matches is not spending."""
    payments(ledger, ("15-Mar-2026", OTHER_SHOP, "45.00"))
    ledger.file(INVOICE, invoice(issued))

    out = ledger.derive()

    assert counts(out) == (2, 0, 0, 1)
    assert len(ledger.txns()) == 2
    assert spending(ledger) == Decimal("-45.00")
    assert sources(ledger, "evidence") == []
    line_count = select(func.count()).select_from(txn_source)
    line_count = line_count.join(
        document_line, document_line.c.id == txn_source.c.document_line_id
    ).where(document_line.c.line_kind == "invoice")
    assert ledger.conn.scalar(line_count) == 0


@pytest.mark.parametrize(
    ("day", "matched"),
    [("31-Dec-2025", True), ("01-Jan-2026", False), ("01-Nov-2025", True)],
    ids=["thirty_days_after", "thirty_one_days_after", "thirty_days_before"],
)
def test_an_invoice_matches_its_payment_inside_thirty_days(
    ledger: Ledger, day: str, matched: bool
) -> None:
    """The invoice total, 750.00, paid to the contractor."""
    payments(ledger, (day, CONTRACTOR, "750.00"))
    ledger.file(INVOICE, invoice())

    out = ledger.derive()

    assert counts(out) == ((2, 1, 0, 0) if matched else (2, 0, 0, 1))
    assert spending(ledger) == Decimal("-750.00")
    links = sources(ledger, "evidence")
    assert [(each.txn_id, each.line_kind) for each in links] == (
        [(txn_of(ledger, CONTRACTOR), "invoice")] if matched else []
    )


def test_an_invoice_and_its_receipt_both_attach_to_one_payment(
    ledger: Ledger,
) -> None:
    """Matching is one to one within each kind: a paid invoice never pushes the
    receipt of the same payment out to the internal account."""
    payments(ledger, ("15-Mar-2026", CLINIC, "45.00"))
    ledger.file(RECEIPT, debit_only_receipt())
    # The clinic's invoice for the same 45.00, paid in full.
    clinic_invoice = changed(
        invoice("Sun, Mar 15, 2026"), "Sample Contractor", "Sample Dental Clinic"
    )
    for printed, amount in (
        ("$40.00   $500.00", "$40.00   $45.00"),
        ("Subtotal   $750.00", "Subtotal   $45.00"),
        ("Total   $750.00", "Total   $45.00"),
        ("Paid   $100.00", "Paid   $45.00"),
        ("Amount due   $650.00", "Amount due   $0.00"),
        ("  Work from Nov 24th to Nov 30th   6.25   $40.00   $250.00\n", ""),
    ):
        clinic_invoice = changed(clinic_invoice, printed, amount)
    ledger.file(INVOICE, clinic_invoice)

    out = ledger.derive()

    assert counts(out) == (2, 2, 0, 0)
    clinic = txn_of(ledger, CLINIC)
    links = sorted(
        (each.line_kind, each.txn_id) for each in sources(ledger, "evidence")
    )
    assert links == [("invoice", clinic), ("receipt", clinic)]
    assert spending(ledger) == Decimal("-45.00")


def test_receipt_and_invoice_lines_store_their_keys(ledger: Ledger) -> None:
    payments(ledger, ("15-Mar-2026", CLINIC, "45.00"))
    ledger.file(RECEIPT, fixture("patient_receipt.txt"))
    ledger.file(INVOICE, invoice())

    ledger.derive()

    rows = ledger.conn.execute(
        select(document_line.c.line_key_id, document_line.c.description_norm).where(
            document_line.c.line_kind.in_(("receipt", "invoice"))
        )
    ).all()
    assert len(rows) == 3
    assert all(row.line_key_id is not None for row in rows)
    assert len({row.line_key_id for row in rows}) == 3
    assert {row.description_norm for row in rows} == {
        "patient receipt: cash", "patient receipt: debit", "invoice sp0042",
    }  # fmt: skip
