"""The owner's ``invoice_paid`` and ``invoice_unpaid`` decisions (FR-8, FR-16, FR-19
path (5); SPEC Derivation stages 3 and 14; edge case 71; AC-8).

Seams: ``finance.decision.record`` and ``finance.decision.reverse`` through
``operations.dispatch``, then ``derive.run`` (the derive ``ledger``), read back as the
txns, their sources, the obligations and the status facts. An invoice is named by its
line key, ``finance.invoice:<key>``. Every date, amount and name is invented.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import uuid
from decimal import Decimal
from typing import Any

import pytest
from pydantic import BaseModel
from sqlalchemy import Row, select

from rheo_finance.clock import FakeClock
from rheo_finance.contracts import Role
from rheo_finance.decision import DecisionRefused, RecordOut, invoice_ref
from rheo_finance.derive.run import run
from rheo_finance.manual.payees import PAYEES_FILE
from rheo_finance.manual.stage2 import account_id
from rheo_finance.operations import dispatch
from rheo_finance.reports.views import (
    INVOICE_DECISION_REFUSED,
    INVOICE_DECISION_SUPERSEDED,
)
from rheo_finance.storage.refs import format_ref
from rheo_finance.storage.schema import (
    document_line,
    obligation,
    payee,
    report_status,
    txn,
    txn_source,
)

from .support import D, Ledger, read_as_text
from .test_receipts_invoices import (
    CLINIC,
    CONTRACTOR,
    INVOICE,
    OTHER_SHOP,
    PAID_OUTSIDE,
    RECEIPT,
    clinic_invoice,
    fixture,
    invoice,
    payments,
    sources,
    spending,
)

TOTAL = Decimal("750.00")
# The fixture prints a total of 750.00, paid 100.00 and an amount due of 650.00.
AMOUNT_DUE = Decimal("650.00")
PAID_ON = D(2026, 3, 10)
DUE_ON = D(2026, 4, 1)
ISSUED = D(2025, 12, 1)


# --- helpers --------------------------------------------------------------------------


def call(ledger: Ledger, name: str, payload: dict[str, object]) -> BaseModel:
    """``name`` through ``dispatch``, then a minute on the fake clock, so each decision
    is newer than the one before it."""
    out = dispatch(name, payload, Role.OWNER, ctx=ledger.ctx)
    assert isinstance(out, BaseModel)
    assert isinstance(ledger.ctx.clock, FakeClock)
    ledger.ctx.clock.advance(dt.timedelta(minutes=1))
    return out


def line_key(ledger: Ledger) -> uuid.UUID:
    """The key of the one invoice of the ledger."""
    keys = set(
        ledger.conn.scalars(
            select(document_line.c.line_key_id).where(
                document_line.c.line_kind == "invoice"
            )
        )
    )
    (key,) = keys
    assert isinstance(key, uuid.UUID)
    return key


def decide(ledger: Ledger, kind: str, **fields: object) -> uuid.UUID:
    out = call(
        ledger,
        "finance.decision.record",
        {"kind": kind, "target_ref": invoice_ref(line_key(ledger)), **fields},
    )
    assert isinstance(out, RecordOut)
    return out.decision_id


def paying_account(slug: str = "paid-outside") -> str:
    return format_ref("finance", "account", account_id(slug))


def paid(
    ledger: Ledger, on: dt.date = PAID_ON, account: str = "paid-outside"
) -> uuid.UUID:
    return decide(
        ledger,
        "invoice_paid",
        account_ref=paying_account(account),
        effective_on=on.isoformat(),
    )


def unpaid(ledger: Ledger, due: dt.date | None = None) -> uuid.UUID:
    fields = {} if due is None else {"effective_on": due.isoformat()}
    return decide(ledger, "invoice_unpaid", **fields)


def reverse(ledger: Ledger, decision_id: uuid.UUID) -> None:
    call(ledger, "finance.decision.reverse", {"decision_id": str(decision_id)})


def outside(ledger: Ledger) -> list[dict[str, Any]]:
    return [row for row in ledger.txns() if row["account_id"] == PAID_OUTSIDE]


def debts(ledger: Ledger) -> list[Row[Any]]:
    return list(ledger.conn.execute(select(obligation)).all())


def facts(ledger: Ledger, name: str) -> dict[str | None, int]:
    found = ledger.conn.execute(
        select(report_status.c.subject, report_status.c.count).where(
            report_status.c.fact == name
        )
    ).all()
    return {subject: count for subject, count in found}


def superseded_facts(ledger: Ledger) -> dict[str | None, int]:
    return facts(ledger, INVOICE_DECISION_SUPERSEDED)


def on_chequing(ledger: Ledger) -> None:
    """The contractor's invoice, derived, with the chequing account on the registry."""
    ledger.chequing()
    ledger.file(INVOICE, invoice())
    ledger.derive()


def open_invoice(ledger: Ledger) -> None:
    """The contractor's invoice with no payment that matches it, derived."""
    payments(ledger, ("15-Mar-2026", OTHER_SHOP, "45.00"))
    ledger.file(INVOICE, invoice())
    ledger.derive()


def ledger_state(ledger: Ledger) -> tuple[list[str], list[str], list[str]]:
    return (
        sorted(repr(tuple(row)) for row in ledger.conn.execute(select(txn))),
        sorted(repr(tuple(row)) for row in ledger.conn.execute(select(txn_source))),
        sorted(repr(tuple(row)) for row in ledger.conn.execute(select(obligation))),
    )


# --- invoice_paid ---------------------------------------------------------------------


def test_an_unmatched_invoice_adds_zero_until_a_decision_adds_exactly_one_line(
    ledger: Ledger,
) -> None:
    open_invoice(ledger)
    assert spending(ledger) == Decimal("-45.00")
    assert outside(ledger) == []

    paid(ledger)
    out = ledger.derive()

    (line,) = outside(ledger)
    assert (line["posting_date"], line["amount"], line["currency"]) == (
        PAID_ON,
        -TOTAL,
        "CAD",
    )
    assert (line["origin"], line["basis"]) == ("document_expense", "unverified_export")
    assert line["counterparty"] == "sample contractor"
    assert line["recognition_date"] == PAID_ON
    assert spending(ledger) == Decimal("-795.00")
    assert (out.lines.from_invoice_decisions, out.lines.unmatched_invoices) == (1, 0)
    assert out.lines.txns == 3
    (primary,) = [row for row in sources(ledger, "primary") if row.txn_id == line["id"]]
    assert (primary.line_kind, primary.line_key_id) == ("invoice", line_key(ledger))


def test_an_old_invoice_counts_at_the_decided_date_not_its_own(ledger: Ledger) -> None:
    payments(ledger, ("15-Mar-2026", OTHER_SHOP, "45.00"))
    ledger.file(INVOICE, invoice("Sun, Dec 1, 2019"))
    ledger.derive()

    paid(ledger, D(2020, 2, 3))
    ledger.derive()

    (line,) = outside(ledger)
    assert line["posting_date"] == D(2020, 2, 3)


def test_a_later_payment_line_supersedes_the_decision_and_counts_once(
    ledger: Ledger,
) -> None:
    ledger.file(INVOICE, invoice())
    ledger.derive()
    decision_id = paid(ledger)
    ledger.derive()
    assert spending(ledger) == -TOTAL
    assert len(outside(ledger)) == 1

    payments(ledger, ("31-Dec-2025", CONTRACTOR, "750.00"))
    out = ledger.derive()

    assert outside(ledger) == []
    assert spending(ledger) == -TOTAL
    assert out.lines.from_invoice_decisions == 0
    assert out.lines.invoice_decisions_superseded == 1
    assert out.lines.unmatched_invoices == 0
    (link,) = sources(ledger, "evidence")
    assert link.line_kind == "invoice"
    assert link.overrides_decision_id == decision_id
    assert superseded_facts(ledger) == {"invoice_paid": 1}


def test_a_payment_near_the_decided_date_supersedes_it_outside_the_invoices_window(
    ledger: Ledger,
) -> None:
    """Paid months after the invoice's date: the decision says when, and the statement
    that arrives later prints that payment. It is one payment, not two."""
    on_chequing(ledger)
    decision_id = paid(ledger, account="chequing")

    payments(ledger, ("12-Mar-2026", CONTRACTOR, "750.00"))
    out = ledger.derive()

    assert outside(ledger) == []
    assert spending(ledger) == -TOTAL
    assert out.lines.invoice_decisions_superseded == 1
    (link,) = sources(ledger, "evidence")
    assert link.overrides_decision_id == decision_id


def test_a_payment_near_the_decided_date_on_another_account_does_not_supersede_it(
    ledger: Ledger,
) -> None:
    """The decision says which account paid. A like payment on another one is another
    payment."""
    on_chequing(ledger)
    paid(ledger, account="paid-outside")

    payments(ledger, ("12-Mar-2026", CONTRACTOR, "750.00"))
    out = ledger.derive()

    assert len(outside(ledger)) == 1
    assert spending(ledger) == -2 * TOTAL
    assert out.lines.invoice_decisions_superseded == 0


def test_a_payment_of_the_amount_due_near_the_decided_date_supersedes_it(
    ledger: Ledger,
) -> None:
    """The invoice printed what was left. A decision over that remainder and the bank
    line that arrives later are one payment."""
    on_chequing(ledger)
    decision_id = paid(ledger, account="chequing")
    ledger.derive()
    assert len(outside(ledger)) == 1

    payments(ledger, ("12-Mar-2026", CONTRACTOR, "650.00"))
    out = ledger.derive()

    assert outside(ledger) == []
    assert spending(ledger) == -AMOUNT_DUE
    assert out.lines.invoice_decisions_superseded == 1
    (link,) = sources(ledger, "evidence")
    assert link.overrides_decision_id == decision_id


def test_a_payment_of_the_amount_due_on_another_account_does_not_supersede_it(
    ledger: Ledger,
) -> None:
    on_chequing(ledger)
    paid(ledger, account="paid-outside")

    payments(ledger, ("12-Mar-2026", CONTRACTOR, "650.00"))
    out = ledger.derive()

    assert len(outside(ledger)) == 1
    assert out.lines.invoice_decisions_superseded == 0


def test_a_payment_of_the_amount_due_matches_only_in_the_decided_round(
    ledger: Ledger,
) -> None:
    """With no decision, the amount due is no match by the invoice's own date."""
    payments(ledger, ("31-Dec-2025", CONTRACTOR, "650.00"))
    ledger.file(INVOICE, invoice())

    out = ledger.derive()

    assert sources(ledger, "evidence") == []
    assert out.lines.unmatched_invoices == 1


def test_a_payment_outside_both_windows_is_another_payment(ledger: Ledger) -> None:
    on_chequing(ledger)
    paid(ledger, account="chequing")

    payments(ledger, ("01-Jun-2026", CONTRACTOR, "750.00"))
    out = ledger.derive()

    assert len(outside(ledger)) == 1
    assert spending(ledger) == -2 * TOTAL
    assert out.lines.invoice_decisions_superseded == 0
    assert superseded_facts(ledger) == {}


def test_a_decided_invoice_does_not_take_a_payment_another_invoice_holds(
    ledger: Ledger,
) -> None:
    """Two invoices of one total and one payment: the payment stays with the one it
    matched, and the decided one is booked."""
    payments(ledger, ("31-Dec-2025", CONTRACTOR, "750.00"))
    ledger.file(INVOICE, invoice())
    ledger.file(
        "invoices-received/sample-vendor/2025-12-02.pdf",
        invoice().replace("SP0042", "SP0043"),
    )
    ledger.derive()
    (matched,) = sources(ledger, "evidence")
    keys = set(
        ledger.conn.scalars(
            select(document_line.c.line_key_id).where(
                document_line.c.line_kind == "invoice"
            )
        )
    )
    (other,) = keys - {matched.line_key_id}
    call(
        ledger,
        "finance.decision.record",
        {
            "kind": "invoice_paid",
            "target_ref": invoice_ref(other),
            "account_ref": paying_account("chequing"),
            "effective_on": "2025-12-30",
        },
    )

    out = ledger.derive()

    assert out.lines.from_invoice_decisions == 1
    assert out.lines.invoice_decisions_superseded == 0
    assert spending(ledger) == -2 * TOTAL


# --- an invoice paid by a cash receipt ------------------------------------------------


def test_an_invoice_on_a_cash_receipt_adds_no_second_paid_outside_line(
    ledger: Ledger,
) -> None:
    """The invoice is evidence of the receipt's txn, whatever the decision says."""
    payments(ledger, ("15-Mar-2026", CLINIC, "45.00"))
    ledger.file(RECEIPT, fixture("patient_receipt.txt"))
    ledger.file(INVOICE, clinic_invoice("200.00", "Sat, Mar 14, 2026"))
    ledger.derive()
    assert spending(ledger) == Decimal("-245.00")

    decision_id = paid(ledger, D(2026, 3, 14))
    out = ledger.derive()

    (line,) = outside(ledger)
    assert line["origin"] == "document_expense"
    assert spending(ledger) == Decimal("-245.00")
    assert out.lines.from_invoice_decisions == 0
    assert out.lines.from_receipts == 1
    (link,) = [row for row in sources(ledger, "evidence") if row.line_kind == "invoice"]
    assert (link.txn_id, link.overrides_decision_id) == (line["id"], decision_id)


def test_a_decision_on_the_internal_account_attaches_to_a_cash_receipt_by_its_date(
    ledger: Ledger,
) -> None:
    """The invoice is months older than the receipt, so only the decided date reaches
    the receipt's txn: still one line on the internal account."""
    payments(ledger, ("15-Mar-2026", CLINIC, "45.00"))
    ledger.file(RECEIPT, fixture("patient_receipt.txt"))
    ledger.file(INVOICE, clinic_invoice("200.00", "Sat, Jan 10, 2026"))
    ledger.derive()
    assert spending(ledger) == Decimal("-245.00")

    decision_id = paid(ledger, D(2026, 3, 14))
    out = ledger.derive()

    (line,) = outside(ledger)
    assert spending(ledger) == Decimal("-245.00")
    assert out.lines.from_invoice_decisions == 0
    (link,) = [row for row in sources(ledger, "evidence") if row.line_kind == "invoice"]
    assert (link.txn_id, link.overrides_decision_id) == (line["id"], decision_id)


def test_a_cash_receipt_filed_after_the_decision_takes_its_place(
    ledger: Ledger,
) -> None:
    payments(ledger, ("15-Mar-2026", CLINIC, "45.00"))
    ledger.file(INVOICE, clinic_invoice("200.00", "Sat, Mar 14, 2026"))
    ledger.derive()
    paid(ledger, D(2026, 3, 20))
    ledger.derive()
    (booked,) = outside(ledger)
    assert booked["posting_date"] == D(2026, 3, 20)

    ledger.file(RECEIPT, fixture("patient_receipt.txt"))
    ledger.derive()

    (line,) = outside(ledger)
    assert line["posting_date"] == D(2026, 3, 14)
    assert spending(ledger) == Decimal("-245.00")


# --- invoice_unpaid -------------------------------------------------------------------


def test_invoice_unpaid_makes_the_manual_debt_with_the_decisions_due_date(
    ledger: Ledger,
) -> None:
    open_invoice(ledger)
    unpaid(ledger, DUE_ON)

    out = ledger.derive()

    (row,) = debts(ledger)
    key = line_key(ledger)
    assert (row.kind, row.period_label) == ("manual_debt", f"debt-inv-{key}")
    assert (row.issued_on, row.due_date) == (ISSUED, DUE_ON)
    assert row.statement_date is None
    assert row.amount_due == row.threshold_due == row.remaining == AMOUNT_DUE
    assert row.status == "open"
    assert row.primary_source == INVOICE
    assert row.manual_source is None
    document_id = ledger.conn.scalar(
        select(document_line.c.document_id).where(document_line.c.line_key_id == key)
    )
    assert row.invoice_ref == f"finance.document:{document_id}"
    # Owed, not spent, and no longer open.
    assert spending(ledger) == Decimal("-45.00")
    assert outside(ledger) == []
    assert out.lines.unmatched_invoices == 0


def test_with_no_printed_amount_due_the_debt_is_the_total(ledger: Ledger) -> None:
    """The stored line is changed, because the one parser always stores one."""
    open_invoice(ledger)
    ledger.conn.execute(
        document_line.update()
        .where(document_line.c.line_kind == "invoice")
        .values(amount_due=None)
    )
    unpaid(ledger)
    ledger.derive()

    (row,) = debts(ledger)
    assert row.amount_due == row.remaining == TOTAL


def test_an_invoice_that_prints_nothing_due_owes_nothing(ledger: Ledger) -> None:
    """A printed zero is an amount due, not a missing one."""
    payments(ledger, ("15-Mar-2026", OTHER_SHOP, "45.00"))
    ledger.file(INVOICE, clinic_invoice("200.00", "Sat, Mar 14, 2026"))
    ledger.derive()
    unpaid(ledger)
    ledger.derive()

    (row,) = debts(ledger)
    assert (row.amount_due, row.status) == (0, "paid")


def test_invoice_unpaid_with_no_due_date_has_none(ledger: Ledger) -> None:
    open_invoice(ledger)
    unpaid(ledger)
    ledger.derive()

    (row,) = debts(ledger)
    assert row.due_date is None
    assert row.issued_on == ISSUED


def test_the_debt_is_owed_to_the_payee_that_names_the_printed_one(
    ledger: Ledger,
) -> None:
    ledger.manual(PAYEES_FILE, "builder,My builder,vendor,SAMPLE CONTRACTOR,,")
    open_invoice(ledger)
    unpaid(ledger)
    ledger.derive()

    (row,) = debts(ledger)
    slug = ledger.conn.scalar(select(payee.c.slug).where(payee.c.id == row.payee_id))
    assert slug == "builder"


def test_with_no_payee_naming_it_the_debt_is_owed_to_the_directorys_vendor(
    ledger: Ledger,
) -> None:
    """The printed name never becomes a label by accident."""
    open_invoice(ledger)
    unpaid(ledger)
    ledger.derive()

    (row,) = debts(ledger)
    found = ledger.conn.execute(
        select(payee.c.slug, payee.c.display_label, payee.c.kind).where(
            payee.c.id == row.payee_id
        )
    ).one()
    assert tuple(found) == ("invoices-received", "invoices-received", "vendor")


def test_two_unpaid_invoices_are_two_debts(ledger: Ledger) -> None:
    ledger.file(INVOICE, invoice())
    ledger.file(
        "invoices-received/sample-vendor/2025-12-02.pdf",
        invoice().replace("SP0042", "SP0043"),
    )
    ledger.derive()
    keys = set(
        ledger.conn.scalars(
            select(document_line.c.line_key_id).where(
                document_line.c.line_kind == "invoice"
            )
        )
    )
    for key in keys:
        call(
            ledger,
            "finance.decision.record",
            {"kind": "invoice_unpaid", "target_ref": invoice_ref(key)},
        )
    ledger.derive()

    assert sorted(row.status for row in debts(ledger)) == ["open", "open"]
    assert sum(row.remaining for row in debts(ledger)) == 2 * AMOUNT_DUE


def test_a_payment_that_matches_takes_the_debt_away(ledger: Ledger) -> None:
    ledger.file(INVOICE, invoice())
    ledger.derive()
    decision_id = unpaid(ledger, DUE_ON)
    ledger.derive()
    assert len(debts(ledger)) == 1

    payments(ledger, ("31-Dec-2025", CONTRACTOR, "750.00"))
    out = ledger.derive()

    assert debts(ledger) == []
    assert spending(ledger) == -TOTAL
    assert out.lines.invoice_decisions_superseded == 1
    (link,) = sources(ledger, "evidence")
    assert link.overrides_decision_id == decision_id
    assert superseded_facts(ledger) == {"invoice_unpaid": 1}


# --- reversal, and two decisions on one invoice ---------------------------------------


@pytest.mark.parametrize("decide_it", [paid, unpaid], ids=["paid", "unpaid"])
def test_reversing_either_decision_restores_the_open_state(
    ledger: Ledger, decide_it: Any
) -> None:
    open_invoice(ledger)
    before = ledger_state(ledger)
    decision_id = decide_it(ledger)
    decided = ledger.derive()
    assert decided.lines.unmatched_invoices == 0
    assert ledger_state(ledger) != before

    reverse(ledger, decision_id)
    out = ledger.derive()

    assert ledger_state(ledger) == before
    assert out.lines.unmatched_invoices == 1
    assert spending(ledger) == Decimal("-45.00")
    assert debts(ledger) == []


def test_the_newest_of_the_two_decisions_holds_and_a_reversal_uncovers_the_other(
    ledger: Ledger,
) -> None:
    open_invoice(ledger)
    paid(ledger)
    newer = unpaid(ledger, DUE_ON)
    ledger.derive()

    assert outside(ledger) == []
    assert len(debts(ledger)) == 1

    reverse(ledger, newer)
    ledger.derive()

    assert debts(ledger) == []
    assert len(outside(ledger)) == 1


def test_a_newer_invoice_paid_takes_an_unpaid_debt_off_what_is_owed(
    ledger: Ledger,
) -> None:
    open_invoice(ledger)
    unpaid(ledger, DUE_ON)
    paid(ledger)
    ledger.derive()

    assert debts(ledger) == []
    assert len(outside(ledger)) == 1


# --- copies, currency and re-derives --------------------------------------------------


def test_a_resaved_copy_of_a_paid_invoice_adds_nothing(ledger: Ledger) -> None:
    text = invoice()
    copy = text.replace("\n", "\r\n")
    ledger.file(INVOICE, text)
    ledger.derive()
    paid(ledger)

    ledger.file("invoices-received/sample-vendor/2025-12-01-copy.pdf", copy)
    ledger.derive()

    (line,) = outside(ledger)
    assert spending(ledger) == -TOTAL
    assert [row.line_kind for row in sources(ledger, "evidence")] == ["invoice"]
    assert line["id"] == line_key(ledger)


def test_a_resaved_copy_keeps_the_debts_key_and_names_the_copy_that_counts(
    ledger: Ledger,
) -> None:
    text = invoice()
    copy = text.replace("\n", "\r\n")
    first = ledger.file(INVOICE, text)
    ledger.derive()
    unpaid(ledger)
    ledger.derive()
    (alone,) = debts(ledger)

    second = ledger.file("invoices-received/sample-vendor/2025-12-01-copy.pdf", copy)
    ledger.derive()

    (row,) = debts(ledger)
    assert row.id == alone.id
    digests = {
        first: hashlib.sha256(text.encode()).hexdigest(),
        second: hashlib.sha256(copy.encode()).hexdigest(),
    }
    counts = max(digests, key=lambda doc: digests[doc])
    assert row.invoice_ref == f"finance.document:{counts}"


def part_paid(amount_paid: str) -> str:
    """The invoice as printed after ``amount_paid``: its key does not read these."""
    total = Decimal(750)
    text = invoice().replace("Paid   $100.00", f"Paid   ${amount_paid}")
    return text.replace(
        "Amount due   $650.00", f"Amount due   ${total - Decimal(amount_paid):.2f}"
    )


@pytest.mark.parametrize(
    "printed",
    [("100.00", "500.00"), ("100.00", "200.00", "500.00")],
    ids=["two", "three"],
)
def test_copies_that_print_different_amounts_due_owe_the_smallest(
    ledger: Ledger, printed: tuple[str, ...]
) -> None:
    """Whichever copy counts: a file's sha256 is no reason for a debt to move."""
    for number, paid_so_far in enumerate(printed):
        ledger.file(
            f"invoices-received/sample-vendor/2025-12-01-{number}.pdf",
            part_paid(paid_so_far),
        )
    ledger.derive()
    unpaid(ledger)
    ledger.derive()

    (row,) = debts(ledger)
    assert row.amount_due == row.remaining == Decimal("250.00")


def test_a_copy_that_prints_no_amount_due_does_not_raise_the_debt(
    ledger: Ledger,
) -> None:
    """A missing figure is not a figure. The stored line is changed, because the one
    parser always stores one."""
    first = ledger.file(INVOICE, invoice())
    copy = "invoices-received/sample-vendor/2025-12-01-copy.pdf"
    ledger.file(copy, part_paid("100.00") + "\n")
    ledger.derive()
    ledger.conn.execute(
        document_line.update()
        .where(document_line.c.document_id == first)
        .values(amount_due=None)
    )
    unpaid(ledger)
    ledger.derive()

    (row,) = debts(ledger)
    assert row.amount_due == AMOUNT_DUE


@pytest.mark.parametrize(
    ("decide_it", "kind"),
    [(paid, "invoice_paid"), (unpaid, "invoice_unpaid")],
    ids=["paid", "unpaid"],
)
def test_a_decided_invoice_in_another_currency_stays_open_and_is_counted(
    ledger: Ledger, decide_it: Any, kind: str
) -> None:
    """Neither the internal account nor an obligation holds it. No parser prints one
    today, so the stored line is changed."""
    ledger.file(INVOICE, invoice())
    ledger.derive()
    ledger.conn.execute(
        document_line.update()
        .where(document_line.c.line_kind == "invoice")
        .values(currency="USD")
    )
    ledger.derive()
    decide_it(ledger)

    out = ledger.derive()

    assert (out.lines.invoices_currency_refused, out.lines.unmatched_invoices) == (1, 1)
    assert ledger.txns() == []
    assert debts(ledger) == []
    assert facts(ledger, INVOICE_DECISION_REFUSED) == {kind: 1}


def test_reversing_a_refused_decision_clears_its_fact(ledger: Ledger) -> None:
    ledger.file(INVOICE, invoice())
    ledger.derive()
    ledger.conn.execute(
        document_line.update()
        .where(document_line.c.line_kind == "invoice")
        .values(currency="USD")
    )
    ledger.derive()
    decision_id = unpaid(ledger)
    ledger.derive()
    assert facts(ledger, INVOICE_DECISION_REFUSED) == {"invoice_unpaid": 1}

    reverse(ledger, decision_id)
    ledger.derive()

    assert facts(ledger, INVOICE_DECISION_REFUSED) == {}


def test_derive_and_rebuild_give_the_same_ledger_under_a_decision(
    ledger: Ledger,
) -> None:
    open_invoice(ledger)
    paid(ledger)
    ledger.derive()
    derived = ledger_state(ledger)

    run(ledger.uow, ledger.ctx, extract_pdf=read_as_text, reparse=True)

    assert ledger_state(ledger) == derived


# --- what the operation refuses -------------------------------------------------------


def record(ledger: Ledger, **fields: object) -> None:
    call(ledger, "finance.decision.record", dict(fields))


def test_invoice_paid_needs_the_paying_account_and_the_date(ledger: Ledger) -> None:
    open_invoice(ledger)
    with pytest.raises(DecisionRefused, match="needs account_ref, effective_on"):
        record(ledger, kind="invoice_paid", target_ref=invoice_ref(line_key(ledger)))


def test_an_invoice_decision_on_anything_but_an_invoice_line_is_refused(
    ledger: Ledger,
) -> None:
    open_invoice(ledger)
    document = format_ref("finance", "document", line_key(ledger))
    with pytest.raises(DecisionRefused, match="target_ref is not an invoice line"):
        record(ledger, kind="invoice_unpaid", target_ref=document)


def test_an_invoice_decision_on_a_key_no_invoice_has_is_refused(ledger: Ledger) -> None:
    """A payment line's key is not an invoice's."""
    open_invoice(ledger)
    (line,) = [row for row in ledger.txns() if row["amount"] == Decimal("-45.00")]
    assert isinstance(line["id"], uuid.UUID)
    with pytest.raises(DecisionRefused, match="resolves to no current invoice line"):
        record(ledger, kind="invoice_unpaid", target_ref=invoice_ref(line["id"]))


def test_invoice_paid_from_an_account_not_on_the_registry_is_refused(
    ledger: Ledger,
) -> None:
    open_invoice(ledger)
    with pytest.raises(DecisionRefused, match="account_ref names no account"):
        record(
            ledger,
            kind="invoice_paid",
            target_ref=invoice_ref(line_key(ledger)),
            account_ref=paying_account("not-registered"),
            effective_on="2026-03-10",
        )
