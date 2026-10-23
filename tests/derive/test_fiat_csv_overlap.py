"""The fiat CSV exports dedupe against the PDFs of the same account and against a
second copy of themselves (FR-4, FR-12(a); AC-80 dedupe clause).

Seam: ``derive.run`` over the synthetic PayPal and Wise CSVs filed beside the PDF
fixtures of the same vendor, read back through ``txn`` and ``txn_source``. The CSV
parsers give the PDF parsers' lines (same sign, same amount and currency per leg, the
fee as its own line), so stage 3's one-to-one matcher (account, currency, signed
amount, a date inside the tolerance) pairs each shared transaction into one txn with
two sources. Every value is synthetic.
"""

from __future__ import annotations

import uuid
from collections import Counter

from sqlalchemy import Table, func, select

from rheo_finance.manual.accounts import ACCOUNTS_FILE, IDENTIFIERS_FILE
from rheo_finance.storage.schema import document, document_line, txn, txn_source

from .support import FIXTURES, D, Ledger

PAYPAL_PDF = "paypal/activity-2026-04-01_to_2026-04-30.pdf"
PAYPAL_CSV = "paypal/activity-2026-04-30.csv"
WISE_PDF = "wise/transactions-2026-02-01_to_2026-02-28.pdf"
WISE_CSV = "wise/statement-2026-02-28.csv"


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def register(
    ledger: Ledger, slug: str, institution: str, kind: str, number: str
) -> None:
    """One two-currency account; its number is what the PDF prints."""
    ledger.manual(
        ACCOUNTS_FILE, f"{slug},{slug.title()},{institution},{kind},CAD;USD,monthly"
        ",,,,,,,,,",
    )  # fmt: skip
    ledger.manual(IDENTIFIERS_FILE, f"{slug},{number},number,,")


def sources_by_txn(ledger: Ledger) -> Counter[uuid.UUID]:
    return Counter(ledger.conn.scalars(select(txn_source.c.txn_id)))


def documents_of(ledger: Ledger, txn_id: uuid.UUID) -> set[uuid.UUID]:
    return set(
        ledger.conn.scalars(
            select(document_line.c.document_id)
            .join(txn_source, txn_source.c.document_line_id == document_line.c.id)
            .where(txn_source.c.txn_id == txn_id)
        )
    )


def count(ledger: Ledger, table: Table) -> int:
    return int(ledger.conn.scalar(select(func.count()).select_from(table)) or 0)


def test_a_paypal_csv_overlapping_the_statement_adds_only_its_own_line(
    ledger: Ledger,
) -> None:
    register(ledger, "paypal", "PayPal", "payment_processor", "SAMPLE999A9ID")
    pdf = ledger.file(PAYPAL_PDF, fixture("paypal_activity.txt"))
    csv = ledger.file(PAYPAL_CSV, fixture("paypal_activity.csv"))

    out = ledger.derive()

    assert out.documents == Counter({"parsed": 2})
    # The statement's 6 lines are txns; the CSV's 6 April lines (the fee line among
    # them) are their second sources, and only its March purchase, before the
    # statement, is a txn of its own.
    assert (out.lines.from_documents, out.lines.additional_sources) == (7, 6)
    assert out.lines.discrepancies == 0
    per_txn = sources_by_txn(ledger)
    assert sorted(per_txn.values()) == [1, 2, 2, 2, 2, 2, 2]
    for txn_id, sources in per_txn.items():
        assert documents_of(ledger, txn_id) == ({pdf, csv} if sources == 2 else {csv})
    rows = ledger.txns()
    assert [row["basis"] for row in rows].count("statement") == 6
    march = [row for row in rows if row["posting_date"] == D(2026, 3, 28)]
    assert [row["basis"] for row in march] == ["verified_export"]


def test_a_wise_csv_and_the_history_pdf_are_one_line_two_sources(
    ledger: Ledger,
) -> None:
    register(ledger, "wise", "Wise", "wallet", "P99900001")
    pdf = ledger.file(WISE_PDF, fixture("wise_history.txt"))
    csv = ledger.file(WISE_CSV, fixture("wise_statement.csv"))

    out = ledger.derive()

    # The CSV prints a balance, so it outranks the unverified history: its 11 lines
    # are txns and every history line, the fee lines and both conversion legs among
    # them, is their second source.
    assert (out.lines.from_documents, out.lines.additional_sources) == (11, 11)
    assert out.lines.discrepancies == 0
    per_txn = sources_by_txn(ledger)
    assert set(per_txn.values()) == {2}
    assert all(documents_of(ledger, each) == {pdf, csv} for each in per_txn)
    assert {row["basis"] for row in ledger.txns()} == {"verified_export"}


def test_a_second_copy_of_a_csv_adds_nothing(ledger: Ledger) -> None:
    register(ledger, "paypal", "PayPal", "payment_processor", "SAMPLE999A9ID")
    text = fixture("paypal_activity.csv")
    ledger.file(PAYPAL_CSV, text)
    ledger.derive()
    before = (count(ledger, document), count(ledger, txn), count(ledger, txn_source))

    ledger.file("paypal/activity-2026-04-30-again.csv", text)
    out = ledger.derive()

    assert before == (1, 7, 7)
    assert (count(ledger, document), count(ledger, txn), count(ledger, txn_source)) == (
        before
    )
    assert out.documents == Counter({"parsed": 1})


def test_an_overlapping_second_export_shares_its_lines(ledger: Ledger) -> None:
    """A later Wise export that drops the oldest rows and adds a new one: the shared
    rows are one txn with two sources, and only the new row is a txn of its own."""
    register(ledger, "wise", "Wise", "wallet", "P99900001")
    header, *rows = fixture("wise_statement.csv").splitlines()
    newest = (
        '"TRANSFER-999000009","28-02-2026","-20.00","USD","Sent money to Sample Payee'
        ' Three","","329.20","","","","","Sample Payee Three","","","","","","","0.00",'
        '""'
    )
    first = ledger.file(WISE_CSV, fixture("wise_statement.csv"))
    # Rows newest first: the later export ends before the two oldest rows.
    later_text = "\n".join([header, newest, *rows[:-2]]) + "\n"
    later = ledger.file("wise/statement-2026-02-28-later.csv", later_text)

    out = ledger.derive()

    assert out.documents == Counter({"parsed": 2})
    # 11 lines in the first file, 10 in the later one: 9 shared, the later file's
    # new row its own txn, the first file's oldest transfer and its fee on their own.
    assert (out.lines.from_documents, out.lines.additional_sources) == (12, 9)
    assert out.lines.discrepancies == 0
    shared = [each for each, n in sources_by_txn(ledger).items() if n == 2]
    assert len(shared) == 9
    assert all(documents_of(ledger, each) == {first, later} for each in shared)
