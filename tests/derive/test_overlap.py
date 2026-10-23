"""Stage 3 precedence and the one-to-one overlap matcher (FR-4, AC-4a, edge cases 7 to
9, Technical Risk 1).

Seams: ``derive.run`` over the synthetic card fixtures (a 2024 export and a transaction
download that overlap; a 2025 download that overlaps two statements and runs past the
last, which a third statement then covers), read back through ``txn``, ``txn_source``,
``source_discrepancy`` and the production snapshot provider. Every value is synthetic.
"""

from __future__ import annotations

import dataclasses
import hashlib
import uuid
from decimal import Decimal

import pytest
from sqlalchemy import ColumnElement, Connection, Table, delete, func, select

from rheo_finance.derive import overlap
from rheo_finance.derive.precedence import STATEMENT, Standing
from rheo_finance.derive.snapshot import txn_snapshot
from rheo_finance.documents.registry import file_archive
from rheo_finance.manual.stage2 import account_id
from rheo_finance.settings import parse_settings
from rheo_finance.storage.refs import format_ref
from rheo_finance.storage.schema import (
    document,
    document_line,
    event,
    source_discrepancy,
    statement_period,
    txn,
    txn_source,
)

from .support import (
    CARD_IDENTIFIER,
    CARD_STATEMENT,
    CARD_TRANSACTIONS,
    CSV_IDENTIFIER,
    FIXTURES,
    NOW,
    STATEMENT_IDENTIFIER,
    D,
    Ledger,
    card_fixture,
)

# name: (archive path, fixture file)
DOCS = {
    "export_2024": ("synthetic-card/2024-03-21-export.csv", "card_2024_export.csv"),
    "download_2024": (
        "synthetic-card/2024-03-06-download.pdf",
        "card_2024_download.txt",
    ),
    "statement_oct": ("synthetic-card/2025-10-05.pdf", "card_statement_2025_10.txt"),
    "statement_nov": ("synthetic-card/2025-11-05.pdf", "card_statement_2025_11.txt"),
    "download_2025": (
        "synthetic-card/2025-11-15-download.pdf",
        "card_2025_download.txt",
    ),
    "statement_dec": ("synthetic-card/2025-12-05.pdf", "card_statement_2025_12.txt"),
    # Two exports of equal rank that overlap and word one purchase differently: the
    # sha256 tie-break decides whose line is the txn.
    "export_2023_a": ("synthetic-card/2023-05-09-a.csv", "card_2023_export_a.csv"),
    "export_2023_b": ("synthetic-card/2023-05-15-b.csv", "card_2023_export_b.csv"),
}
NAMES = tuple(DOCS)
# Every rotation of the declared order and of its reverse: 16 orders, each document
# first in two of them.
ORDERS = [
    list(names[i:] + names[:i]) for names in (NAMES, NAMES[::-1]) for i in range(8)
]


def sha(name: str) -> str:
    return hashlib.sha256(card_fixture(DOCS[name][1]).encode("utf-8")).hexdigest()


def file(ledger: Ledger, name: str) -> uuid.UUID:
    path, fixture = DOCS[name]
    return ledger.file(path, card_fixture(fixture))


def count(conn: Connection, table: Table, *where: ColumnElement[bool]) -> int:
    return int(conn.scalar(select(func.count()).select_from(table).where(*where)) or 0)


def contents(conn: Connection) -> dict[str, list[str]]:
    """Every row of the ledger tables and every document's status, comparable."""
    tables = (txn, txn_source, source_discrepancy)
    found = {
        table.name: sorted(repr(tuple(row)) for row in conn.execute(select(table)))
        for table in tables
    }
    found["document"] = sorted(
        repr(tuple(row))
        for row in conn.execute(select(document.c.sha256, document.c.status))
    )
    return found


def file_all_at_once(ledger: Ledger) -> None:
    """Forget every document, then register the whole archive in one walk and derive
    once: importing the union."""
    for table in (
        txn_source, txn, source_discrepancy, document_line, statement_period, event,
        document,
    ):  # fmt: skip
        ledger.conn.execute(delete(table))
    file_archive(ledger.uow, ledger.archive, ledger.data, NOW)
    ledger.derive()


def card_text(
    header: str,
    *lines: str,
    period: str = "",
    opening: str = "0",
    currency: str = "CAD",
) -> str:
    """A card document in the synthetic layout; a statement's closing is its opening
    plus its lines."""
    rows = [header, f"ACCOUNT {CARD_IDENTIFIER}", f"CURRENCY {currency}"]
    if header == CARD_STATEMENT:
        total = Decimal(opening) + sum(
            (Decimal(line.split(" ")[2]) for line in lines), Decimal(0)
        )
        rows += [f"PERIOD {period}", f"OPENING {opening}", f"CLOSING {total}"]
    rows += [f"LINE {line}" for line in lines]
    return "\n".join(rows) + "\n"


def txn_rows(conn: Connection) -> list[dict[str, object]]:
    return [
        dict(row)
        for row in conn.execute(
            select(txn).order_by(txn.c.posting_date, txn.c.description_norm)
        ).mappings()
    ]


def joined(conn: Connection, line_no: int, document_id: uuid.UUID) -> object:
    """The posting date of the txn a document's line is a source of."""
    return conn.scalar(
        select(txn.c.posting_date)
        .join(txn_source, txn_source.c.txn_id == txn.c.id)
        .join(document_line, document_line.c.id == txn_source.c.document_line_id)
        .where(
            document_line.c.document_id == document_id,
            document_line.c.line_no == line_no,
        )
    )


@pytest.mark.parametrize("order", ORDERS, ids=lambda o: "-".join(n[:3] for n in o))
def test_import_order_does_not_change_the_ledger(
    ledger: Ledger, card_parsers: None, order: list[str]
) -> None:
    ledger.card()
    for name in order:
        file(ledger, name)
        ledger.derive()
    in_order = contents(ledger.conn)

    file_all_at_once(ledger)

    assert contents(ledger.conn) == in_order
    # And the union is the right ledger: 3 txns in 2023, 8 in 2024 and 9 in 2025,
    # every overlap line a source, one discrepancy, nothing provisional once December
    # is in.
    assert count(ledger.conn, txn) == 20
    assert count(ledger.conn, txn_source) == 32
    assert count(ledger.conn, txn_source, txn_source.c.role == "additional") == 12
    assert count(ledger.conn, source_discrepancy) == 1
    assert count(ledger.conn, txn, txn.c.provisional) == 0
    # The diner purchase is one txn, keyed by the line of the export with the larger
    # sha256; the other export's line is its additional source.
    larger = max(("export_2023_a", "export_2023_b"), key=lambda name: sha(name))
    diner = ledger.conn.execute(
        select(txn.c.id).where(txn.c.posting_date == D(2023, 5, 2))
    ).scalar_one()
    primary_line = ledger.conn.scalar(
        select(document_line.c.line_key_id)
        .join(document, document.c.id == document_line.c.document_id)
        .where(document.c.sha256 == sha(larger), document_line.c.line_no == 1)
    )
    assert diner == primary_line


def test_overlapping_exports_with_no_statement_share_their_lines(
    ledger: Ledger, card_parsers: None
) -> None:
    ledger.card()
    csv = file(ledger, "export_2024")
    pdf = file(ledger, "download_2024")

    out = ledger.derive()

    # The download outranks the CSV: its 5 lines are txns; of the CSV's 7, the twin
    # cafe lines, the pharmacy line (a day apart, worded differently) and the payment
    # (worded differently) join them, and 3 lines only the CSV has become txns.
    assert (out.lines.from_documents, out.lines.additional_sources) == (8, 4)
    assert out.lines.discrepancies == 0
    rows = txn_rows(ledger.conn)
    assert {row["basis"] for row in rows} == {"unverified_export"}
    assert not any(row["provisional"] for row in rows)
    twins = [row for row in rows if row["posting_date"] == D(2024, 2, 20)]
    assert len(twins) == 2
    assert joined(ledger.conn, 3, csv) == joined(ledger.conn, 2, pdf) == D(2024, 2, 20)
    assert joined(ledger.conn, 5, csv) == D(2024, 2, 26)  # the download's date
    # Occurrences count within a document, so the twins carry the same two keys in
    # both documents.
    keys = {
        doc: set(
            ledger.conn.scalars(
                select(document_line.c.line_key_id).where(
                    document_line.c.document_id == doc,
                    document_line.c.posting_date == D(2024, 2, 20),
                )
            )
        )
        for doc in (csv, pdf)
    }
    assert keys[csv] == keys[pdf]
    assert len(keys[csv]) == 2


def test_a_transaction_date_counts_as_a_date_of_the_line(
    ledger: Ledger, card_parsers: None
) -> None:
    # Posted four days after the purchase: the export's date is the statement line's
    # transaction date, so they match although the posting dates are four days apart.
    ledger.card()
    ledger.file(
        "synthetic-card/2025-02-05.pdf",
        card_text(
            CARD_STATEMENT,
            "2025-01-14 2025-01-10 -10.00 SAMPLE ALPHA 99943",
            period="2025-01-06 2025-02-05",
        ),
    )
    ledger.file(
        "synthetic-card/2025-02-05-export.csv",
        card_text(CARD_TRANSACTIONS, "2025-01-10 - -10.00 SAMPLE ALPHA 99943"),
    )

    out = ledger.derive()

    assert (out.lines.txns, out.lines.additional_sources) == (1, 1)
    assert out.lines.discrepancies == 0


def test_an_export_line_inside_a_statement_period_that_matches_nothing_is_a_discrepancy(
    ledger: Ledger, card_parsers: None
) -> None:
    ledger.card()
    file(ledger, "statement_oct")
    file(ledger, "statement_nov")
    download = file(ledger, "download_2025")

    out = ledger.derive()

    assert out.lines.discrepancies == 1
    row = ledger.conn.execute(
        select(
            source_discrepancy, document_line.c.line_no, document_line.c.amount
        ).join(
            document_line, document_line.c.id == source_discrepancy.c.document_line_id
        )
    ).one()
    assert row.kind == "unmatched_in_statement_window"
    assert (row.document_id, row.line_no) == (download, 5)
    assert row.account_id == account_id("card")
    # The line was not added to the ledger.
    assert count(ledger.conn, txn, txn.c.amount == row.amount) == 0
    # Six statement lines are txns; the download's lines inside the statements'
    # periods (one dated across the boundary of the two) join them.
    assert out.lines.additional_sources == 5
    assert joined(ledger.conn, 3, download) == D(2025, 10, 7)


def test_a_provisional_line_becomes_statement_backed_without_a_duplicate(
    ledger: Ledger, card_parsers: None
) -> None:
    ledger.card()
    for name in ("statement_oct", "statement_nov", "download_2025"):
        file(ledger, name)
    ledger.derive()
    provisional = [row for row in txn_rows(ledger.conn) if row["provisional"]]
    assert [(row["posting_date"], row["basis"]) for row in provisional] == [
        (D(2025, 11, 8), "unverified_export"),
        (D(2025, 11, 15), "unverified_export"),
    ]
    refs = [format_ref("finance", "txn", str(row["id"])) for row in provisional]
    assert count(ledger.conn, txn) == 8

    file(ledger, "statement_dec")
    ledger.derive()

    assert count(ledger.conn, txn) == 9
    assert count(ledger.conn, txn, txn.c.provisional) == 0
    # Each reference taken on a provisional line now resolves to the statement line,
    # which has the download line as its second source.
    snapshots = [txn_snapshot(ledger.uow, ref) for ref in refs]
    assert [(s.posting_date, s.amount) for s in snapshots if s] == [
        (D(2025, 11, 9), Decimal("-22.00")),
        (D(2025, 11, 15), Decimal("-33.00")),
    ]
    for posting in (D(2025, 11, 9), D(2025, 11, 15)):
        row = ledger.conn.execute(
            select(txn.c.id, txn.c.basis).where(txn.c.posting_date == posting)
        ).one()
        assert row.basis == "statement"
        roles = ledger.conn.scalars(
            select(txn_source.c.role).where(txn_source.c.txn_id == row.id)
        ).all()
        assert sorted(roles) == ["additional", "primary"]


def test_matching_is_one_to_one(ledger: Ledger, card_parsers: None) -> None:
    # The statement has one cafe line; the export has it twice, worded differently.
    # One joins it; the other is inside the statement's period and is a discrepancy.
    ledger.card()
    ledger.file(
        "synthetic-card/2025-02-05.pdf",
        card_text(
            CARD_STATEMENT,
            "2025-01-10 - -4.50 SAMPLE CAFE 99941",
            period="2025-01-06 2025-02-05",
        ),
    )
    ledger.file(
        "synthetic-card/2025-02-05-export.csv",
        card_text(
            CARD_TRANSACTIONS,
            "2025-01-10 - -4.50 SAMPLE CAFE #99941",
            "2025-01-10 - -4.50 SAMPLE CAFE #99941",
        ),
    )

    out = ledger.derive()

    assert (out.lines.txns, out.lines.additional_sources) == (1, 1)
    assert out.lines.discrepancies == 1


def test_identical_lines_of_two_documents_pair_off_by_occurrence(
    ledger: Ledger, card_parsers: None
) -> None:
    ledger.card()
    ledger.file(
        "synthetic-card/2025-02-05.pdf",
        card_text(
            CARD_STATEMENT,
            "2025-01-10 - -4.50 SAMPLE CAFE 99941",
            "2025-01-10 - -4.50 SAMPLE CAFE 99941",
            period="2025-01-06 2025-02-05",
        ),
    )
    ledger.file(
        "synthetic-card/2025-02-05-export.csv",
        card_text(
            CARD_TRANSACTIONS,
            "2025-01-10 - -4.50 SAMPLE CAFE 99941",
            "2025-01-10 - -4.50 SAMPLE CAFE 99941",
        ),
    )

    out = ledger.derive()

    assert (out.lines.txns, out.lines.additional_sources) == (2, 2)
    # Each export line carries the key of the statement line it joined.
    pairs = ledger.conn.execute(
        select(txn_source.c.txn_id, txn_source.c.line_key_id).where(
            txn_source.c.role == "additional"
        )
    ).all()
    assert all(txn_id == key for txn_id, key in pairs)


def test_a_statement_line_outside_its_own_period_is_not_a_source_of_its_sibling(
    ledger: Ledger, card_parsers: None
) -> None:
    # A line dated the day before the period starts is not the statement's own: it is
    # matched, but never against the statement's other lines.
    ledger.card()
    ledger.file(
        "synthetic-card/2025-02-05.pdf",
        card_text(
            CARD_STATEMENT,
            "2025-01-05 - -4.50 SAMPLE CAFE 99941",
            "2025-01-06 - -4.50 SAMPLE CAFE 99941",
            period="2025-01-06 2025-02-05",
        ),
    )

    out = ledger.derive()

    assert (out.lines.txns, out.lines.additional_sources) == (2, 0)


def test_statements_do_not_match_each_other_across_a_period_boundary(
    ledger: Ledger, card_parsers: None
) -> None:
    # The same coffee on the last day of one period and the first of the next: two
    # purchases, each owned by its own statement.
    ledger.card()
    for period, day in (("2025-01-06 2025-02-05", "2025-02-05"),
                        ("2025-02-06 2025-03-05", "2025-02-06")):  # fmt: skip
        ledger.file(
            f"synthetic-card/{period[-10:]}.pdf",
            card_text(
                CARD_STATEMENT, f"{day} - -4.50 SAMPLE CAFE 99942", period=period
            ),
        )

    out = ledger.derive()

    assert (out.lines.txns, out.lines.additional_sources) == (2, 0)


def test_an_exact_date_wins_then_the_description_then_the_earliest(
    ledger: Ledger, card_parsers: None
) -> None:
    ledger.card()
    ledger.file(
        "synthetic-card/2025-02-05.pdf",
        card_text(
            CARD_STATEMENT,
            "2025-01-10 - -10.00 SAMPLE ALPHA 99943",
            "2025-01-11 - -10.00 SAMPLE BETA 99944",
            "2025-01-20 - -7.00 SAMPLE GAMMA 99945",
            "2025-01-22 - -7.00 SAMPLE DELTA 99946",
            "2025-01-24 - -3.00 SAMPLE EPSILON 99947",
            "2025-01-26 - -3.00 SAMPLE ZETA 99948",
            period="2025-01-06 2025-02-05",
        ),
    )
    export = ledger.file(
        "synthetic-card/2025-02-05-export.csv",
        card_text(
            CARD_TRANSACTIONS,
            # Alpha's wording on Beta's date: the exact date wins.
            "2025-01-11 - -10.00 SAMPLE ALPHA 99943",
            # A day from both: Delta's wording wins over the earlier Gamma.
            "2025-01-21 - -7.00 SAMPLE DELTA 99946",
            # Two days from both, neither wording: the earliest.
            "2025-01-25 - -3.00 SAMPLE OTHER 99949",
        ),
    )

    out = ledger.derive()

    assert out.lines.additional_sources == 3
    assert joined(ledger.conn, 1, export) == D(2025, 1, 11)
    assert joined(ledger.conn, 2, export) == D(2025, 1, 22)
    assert joined(ledger.conn, 3, export) == D(2025, 1, 24)


def test_dates_further_apart_than_the_tolerance_do_not_match(
    ledger: Ledger, card_parsers: None
) -> None:
    ledger.card()
    ledger.file(
        "synthetic-card/2025-01-20-download.pdf",
        card_text(CARD_TRANSACTIONS, "2025-01-10 - -10.00 SAMPLE ALPHA 99943"),
    )
    ledger.file(
        "synthetic-card/2025-01-20-export.csv",
        card_text(CARD_TRANSACTIONS, "2025-01-14 - -10.00 SAMPLE ALPHA 99943"),
    )

    # Four days apart: two purchases under the default of 3 days.
    assert ledger.derive().lines.txns == 2

    ledger.ctx = dataclasses.replace(
        ledger.ctx,
        settings=parse_settings(
            f'[finance.paths]\narchive = "{ledger.archive}"\n'
            "[finance.import]\noverlap_day_tolerance = 4\n"
        ),
    )
    assert ledger.derive().lines.txns == 1


def test_a_line_whose_key_names_a_txn_already_taken_is_a_discrepancy(
    ledger: Ledger, card_parsers: None
) -> None:
    # The download has one cafe line; the CSV has it a day later, and joins it. A third
    # document has both wordings: the first joins the txn; the second's key names that
    # same txn, already taken, so it can be neither a source of another txn nor a new
    # one under that key.
    ledger.card()
    ledger.file(
        "synthetic-card/2025-01-20-download.pdf",
        card_text(CARD_TRANSACTIONS, "2025-01-10 - -4.50 SAMPLE CAFE 99941"),
    )
    ledger.file(
        "synthetic-card/2025-01-20-export.csv",
        card_text(CARD_TRANSACTIONS, "2025-01-11 - -4.50 SAMPLE CAFE 99941"),
    )
    third = ledger.file(
        "synthetic-card/2025-01-20-other.txt",
        card_text(
            CARD_TRANSACTIONS,
            "2025-01-10 - -4.50 SAMPLE CAFE 99941",
            "2025-01-11 - -4.50 SAMPLE CAFE 99941",
        ),
    )

    out = ledger.derive()

    assert (out.lines.txns, out.lines.additional_sources) == (1, 2)
    row = ledger.conn.execute(
        select(source_discrepancy.c.kind, document_line.c.line_no).join(
            document_line, document_line.c.id == source_discrepancy.c.document_line_id
        )
    ).one()
    assert tuple(row) == ("line_key_taken", 2)
    assert ledger.conn.scalar(select(source_discrepancy.c.document_id)) == third


def test_each_txn_carries_the_basis_of_its_first_source(
    ledger: Ledger, card_parsers: None
) -> None:
    ledger.accounts(
        ("chequing", "Vancity", "chequing", CSV_IDENTIFIER),
        ("card", "Sample Card", "credit_card", CARD_IDENTIFIER),
    )
    ledger.add_identifier("chequing", STATEMENT_IDENTIFIER)
    for path, fixture in (
        ("vancity/export.csv", "vancity_chequing_jump.csv"),
        ("vancity/2026-01-10.pdf", "vancity_statement_split.txt"),
    ):
        ledger.file(path, (FIXTURES / fixture).read_text(encoding="utf-8"))
    file(ledger, "download_2024")
    ledger.cash("paid-outside,2026-02-01,-12.00,CAD,coffee money,")

    ledger.derive()

    bases = {(row["origin"], row["basis"]) for row in txn_rows(ledger.conn)}
    assert bases == {
        ("document_line", "statement"),
        ("document_line", "verified_export"),
        ("document_line", "unverified_export"),
        ("manual", "manual"),
    }
    # The chequing export is dated after the chequing statement ends: provisional.
    chequing = account_id("chequing")
    rows = [row for row in txn_rows(ledger.conn) if row["account_id"] == chequing]
    assert {(row["basis"], row["provisional"]) for row in rows} == {
        ("statement", False),
        ("verified_export", True),
    }
    # ISO dates compare as text.
    assert all(
        str(row["posting_date"]) > "2026-01-10" for row in rows if row["provisional"]
    )


def test_an_export_listed_newest_first_matches_its_lines_oldest_first(
    ledger: Ledger, card_parsers: None
) -> None:
    # The statement ends on 5 Dec with a cafe purchase posted that day. The export,
    # newest first, has a new cafe purchase on 7 Dec and the statement's one dated by
    # its purchase day, 3 Dec. Both are two days from the statement line: the older
    # one is the statement's purchase, and the newer one is a provisional txn.
    ledger.card()
    ledger.file(
        "synthetic-card/2025-12-05.pdf",
        card_text(
            CARD_STATEMENT,
            "2025-12-05 - -4.50 SAMPLE CAFE 99941",
            period="2025-11-06 2025-12-05",
        ),
    )
    export = ledger.file(
        "synthetic-card/2025-12-08-export.csv",
        card_text(
            CARD_TRANSACTIONS,
            "2025-12-07 - -4.50 SAMPLE CAFE 99941",
            "2025-12-03 - -4.50 SAMPLE CAFE 99941",
        ),
    )

    out = ledger.derive()

    assert (out.lines.txns, out.lines.additional_sources) == (2, 1)
    assert out.lines.discrepancies == 0
    assert joined(ledger.conn, 2, export) == D(2025, 12, 5)
    newer = ledger.conn.execute(
        select(txn.c.provisional).where(txn.c.posting_date == D(2025, 12, 7))
    ).scalar_one()
    assert newer is True


def test_a_statement_period_owns_only_lines_of_its_own_currency(
    ledger: Ledger, card_parsers: None
) -> None:
    # A tied-out USD period covers October; no CAD statement does. CAD export lines in
    # October and November are ordinary txns, neither provisional nor a discrepancy,
    # while a USD export line in October that matches nothing is a discrepancy.
    ledger.card()
    ledger.file(
        "synthetic-card/2025-10-31-usd.pdf",
        card_text(
            CARD_STATEMENT,
            "2025-10-03 - -20.00 SAMPLE STORE 99971",
            period="2025-10-01 2025-10-31",
            currency="USD",
        ),
    )
    ledger.file(
        "synthetic-card/2025-10-20-cad.csv",
        card_text(
            CARD_TRANSACTIONS,
            "2025-10-12 - -9.00 SAMPLE KIOSK 99972",
            "2025-11-12 - -9.00 SAMPLE KIOSK 99972",
        ),
    )
    ledger.file(
        "synthetic-card/2025-10-20-usd.csv",
        card_text(
            CARD_TRANSACTIONS,
            "2025-10-14 - -6.00 SAMPLE KIOSK 99973",
            currency="USD",
        ),
    )

    out = ledger.derive()

    assert (out.lines.txns, out.lines.discrepancies) == (3, 1)
    # Neither CAD line is provisional: the account has no CAD statement for them to
    # come after, though the November one is after the USD statement.
    cad = ledger.conn.execute(
        select(txn.c.provisional).where(txn.c.currency == "CAD")
    ).all()
    assert [tuple(row) for row in cad] == [(False,), (False,)]


def test_a_reissued_statement_of_one_period_does_not_double_its_lines(
    ledger: Ledger, card_parsers: None
) -> None:
    # Two tied-out statements of one period that differ (a corrected reissue): one
    # words the cafe line differently and adds a kiosk line. Neither is a copy, so
    # neither is superseded; the first in rank owns the period and the other's lines
    # are matched against it.
    ledger.card()
    period = "2025-01-06 2025-02-05"
    first = card_text(
        CARD_STATEMENT,
        "2025-01-10 - -4.50 SAMPLE CAFE 99941",
        "2025-01-20 - -30.00 SAMPLE FUEL 99942",
        period=period,
    )
    second = card_text(
        CARD_STATEMENT,
        "2025-01-10 - -4.50 SAMPLE CAFE #99941",
        "2025-01-20 - -30.00 SAMPLE FUEL 99942",
        "2025-01-25 - -8.00 SAMPLE KIOSK 99943",
        period=period,
    )
    ledger.file("synthetic-card/2025-02-05-a.pdf", first)
    ledger.file("synthetic-card/2025-02-05-b.pdf", second)

    out = ledger.derive()

    assert out.documents == {"parsed": 2}
    assert out.lines.additional_sources == 2
    for amount in ("-4.50", "-30.00"):
        assert count(ledger.conn, txn, txn.c.amount == Decimal(amount)) == 1
    hashes = {
        text: hashlib.sha256(text.encode()).hexdigest() for text in (first, second)
    }
    if hashes[first] > hashes[second]:
        # The first owns the period: the kiosk line matches nothing inside it.
        assert (out.lines.txns, out.lines.discrepancies) == (2, 1)
    else:
        assert (out.lines.txns, out.lines.discrepancies) == (3, 0)


def test_a_statement_with_a_period_per_currency_owns_each_in_its_own_currency() -> None:
    # One statement summarizes CAD for September and USD for October (``match_lines``
    # directly: no fixture parser prints two currencies). A CAD export line in October
    # is outside every CAD period, so it is a txn.
    account, statement, export = (uuid.uuid4() for _ in range(3))
    standings = {
        account: [
            Standing(
                statement, account, STATEMENT, 0, "9" * 64,
                frozenset({("CAD", D(2025, 9, 1), D(2025, 9, 30)),
                           ("USD", D(2025, 10, 1), D(2025, 10, 31))}),
            ),
            Standing(export, account, 2, 2, "8" * 64, frozenset()),
        ]
    }  # fmt: skip
    line = overlap.Line(
        id=uuid.uuid4(),
        document_id=export,
        account_id=account,
        posting_date=D(2025, 10, 12),
        txn_date=None,
        amount=Decimal("-9.00"),
        currency="CAD",
        description_norm="sample kiosk 99972",
        key=uuid.uuid4(),
    )

    outcome = overlap.match_lines(standings, {(account, export): [line]}, 3)

    assert [commit.line for commit in outcome.commits] == [line]
    assert outcome.discrepancies == []
