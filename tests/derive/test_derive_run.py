"""``derive.run``: documents and manual lines become txns (stages 1 to 3).

Seams: ``run`` over fixture documents and manual files, read back through the tables
it fills. Every value is synthetic; a "PDF" is layout text.
"""

from __future__ import annotations

import csv
import dataclasses
import datetime as dt
import uuid
from collections.abc import Callable
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import Connection, Table, delete, func, insert, select, update
from sqlalchemy.exc import IntegrityError

from rheo_finance.context import UnitOfWork
from rheo_finance.derive import run as derive_run
from rheo_finance.derive import stage3
from rheo_finance.derive.stage3 import CURRENCY_REFUSED, Lines, build_lines
from rheo_finance.documents.registry import DocumentRefused
from rheo_finance.manual.cash_lines import CashLines
from rheo_finance.manual.stage2 import account_id
from rheo_finance.parsers.registry import DOCUMENT_REJECTED
from rheo_finance.settings import parse_settings
from rheo_finance.sinks.spec import SINK_SPECS, RequestTemplate, SinkSpec, spec_digest
from rheo_finance.storage import schema
from rheo_finance.storage.ids import derived_id, uuid7
from rheo_finance.storage.schema import (
    account,
    decision,
    document,
    document_line,
    event,
    manual_entry_error,
    sink_spec,
    statement_period,
    txn,
    txn_source,
)

from .support import FIXTURES, D, Ledger, csv_row, read_as_text

CSV_FIXTURE = (FIXTURES / "vancity_chequing_jump.csv").read_text(encoding="utf-8")
STATEMENT = (FIXTURES / "vancity_statement_split.txt").read_text(encoding="utf-8")
GROCER = "POS PURCHASE                 SAMPLE GROCER          99901   "


def count(conn: Connection, table: Table) -> int:
    return int(conn.scalar(select(func.count()).select_from(table)) or 0)


def test_every_line_of_a_document_becomes_a_txn_with_its_source(ledger: Ledger) -> None:
    ledger.chequing()
    doc = ledger.file("vancity/export.csv", CSV_FIXTURE)

    out = ledger.derive()

    assert out.documents == {"parsed": 1}
    assert out.lines.from_documents == 6
    rows = ledger.txns()
    assert len(rows) == 6
    assert {row["currency"] for row in rows} == {"CAD"}
    assert all(row["origin"] == "document_line" for row in rows)
    assert all(row["movement_kind"] == "fiat" for row in rows)
    # Signed exact amounts: money out is negative.
    amounts = sorted(row["amount"] for row in rows)  # type: ignore[type-var]
    assert amounts == sorted(
        Decimal(each)
        for each in ("-9.99", "75.00", "-30.00", "-45.50", "-20.00", "500.00")
    )
    # The id is the key of the line's natural key on its posting date.
    grocer = next(row for row in rows if row["posting_date"] == D(2026, 2, 16))
    norm = "pos purchase sample grocer 99901"
    assert grocer["description_norm"] == norm
    assert grocer["id"] == derived_id(
        D(2026, 2, 16),
        ("fiat", "chequing", D(2026, 2, 16), Decimal("-9.99"), "CAD", norm, 1),
    )
    # Every txn has exactly one primary source, a line of this document, whose key is
    # the txn's id and is stored back on the line.
    sources = (
        ledger.conn.execute(
            select(
                txn_source,
                document_line.c.document_id,
                document_line.c.line_key_id.label("kept"),
            ).join(document_line, document_line.c.id == txn_source.c.document_line_id)
        )
        .mappings()
        .all()
    )
    assert len(sources) == 6
    assert {row["role"] for row in sources} == {"primary"}
    assert {row["document_id"] for row in sources} == {doc}
    assert all(row["line_key_id"] == row["txn_id"] == row["kept"] for row in sources)
    assert all(row["manual_ref"] is None for row in sources)
    # The normalized description is stored back on the line too.
    kept_norms = dict(
        ledger.conn.execute(
            select(document_line.c.line_key_id, document_line.c.description_norm)
        ).all()
    )
    assert kept_norms == {row["id"]: row["description_norm"] for row in rows}


def test_a_pdf_statement_becomes_txns_too(ledger: Ledger) -> None:
    ledger.chequing()
    ledger.file("vancity/2026-01-10.pdf", STATEMENT)

    out = ledger.derive()

    assert out.documents == {"parsed": 1}
    rows = ledger.txns()
    assert sorted((r["posting_date"], r["amount"]) for r in rows) == [
        (D(2025, 12, 15), Decimal("-2.00")),
        (D(2025, 12, 15), Decimal("50.00")),
        (D(2025, 12, 20), Decimal("-30.00")),
    ]


def test_two_identical_same_day_lines_both_survive(ledger: Ledger) -> None:
    ledger.chequing()
    same = csv_row("03-Feb-2026", GROCER, out="10.00", balance="80.00")
    ledger.file(
        "vancity/two.csv",
        "\n".join(
            [
                csv_row(
                    "04-Feb-2026",
                    "DEPOSIT   SAMPLE PAYROLL  99906",
                    into="5.00",
                    balance="90.00",
                ),
                same,
                same.replace("80.00", "90.00"),
                csv_row(
                    "02-Feb-2026",
                    "DEPOSIT   SAMPLE PAYROLL  99906",
                    into="100.00",
                    balance="100.00",
                ),
            ]
        )
        + "\n",
    )

    out = ledger.derive()

    assert out.lines.from_documents == 4
    twins = [r for r in ledger.txns() if r["posting_date"] == D(2026, 2, 3)]
    assert len(twins) == 2
    assert twins[0]["id"] != twins[1]["id"]
    norm = "pos purchase sample grocer 99901"
    assert {row["id"] for row in twins} == {
        derived_id(
            D(2026, 2, 3),
            ("fiat", "chequing", D(2026, 2, 3), Decimal("-10.00"), "CAD", norm, n),
        )
        for n in (1, 2)
    }


def test_running_derive_again_gives_the_same_ledger(ledger: Ledger) -> None:
    ledger.chequing()
    ledger.file("vancity/export.csv", CSV_FIXTURE)
    ledger.file("vancity/2026-01-10.pdf", STATEMENT)
    ledger.cash("paid-outside,2026-02-01,-12.00,CAD,coffee money,")
    ledger.derive()
    before = ledger.txns()
    sources = count(ledger.conn, txn_source)

    again = ledger.derive()

    assert again.lines.txns == len(before) == 10
    assert ledger.txns() == before
    assert count(ledger.conn, txn_source) == sources == 10
    assert count(ledger.conn, txn) == 10


def test_a_manual_cash_line_becomes_a_txn_with_its_source_reference(
    ledger: Ledger,
) -> None:
    ledger.chequing()
    ledger.cash(
        "paid-outside,2026-02-01,-12.00,CAD,Coffee   Money,from the jar",
        "paid-outside,2026-02-01,-12.00,CAD,coffee money,",
    )

    out = ledger.derive()

    assert (out.lines.from_manual, out.lines.manual_currency_refused) == (2, 0)
    rows = ledger.txns()
    assert [(r["origin"], r["basis"], r["amount"]) for r in rows] == [
        ("manual", "manual", Decimal("-12.00")),
    ] * 2
    assert {r["description_norm"] for r in rows} == {"coffee money"}
    pairs = ledger.conn.execute(
        select(
            txn_source.c.manual_ref, txn_source.c.txn_id, txn_source.c.role
        ).order_by(txn_source.c.manual_ref)
    ).all()
    assert [(p.manual_ref, p.role) for p in pairs] == [
        ("manual:transactions.csv:2", "primary"),
        ("manual:transactions.csv:3", "primary"),
    ]
    assert {p.txn_id for p in pairs} == {r["id"] for r in rows}
    assert rows[0]["id"] != rows[1]["id"]


def test_a_manual_line_matching_a_statement_line_keeps_both_ids(
    ledger: Ledger,
) -> None:
    ledger.chequing()
    ledger.file("vancity/export.csv", CSV_FIXTURE)
    ledger.derive()
    alone = {row["id"] for row in ledger.txns()}

    # The same account, day, amount and wording as the 16 Feb grocer line.
    ledger.cash("chequing,2026-02-16,-9.99,CAD,pos purchase sample grocer 99901,")
    out = ledger.derive()

    assert (out.lines.from_documents, out.lines.from_manual) == (6, 1)
    rows = ledger.txns()
    assert len(rows) == 7
    # Statement lines keep their ids; the manual line has an id of its own.
    assert alone < {row["id"] for row in rows}


def test_an_unreadable_archived_file_is_unparsed_and_the_others_derive(
    ledger: Ledger,
) -> None:
    # A cell over the CSV reader's size limit: the file is not read (FR-9), and the
    # good export next to it still becomes txns.
    ledger.chequing()
    giant = CSV_FIXTURE.replace("POS PURCHASE", "x" * 200_000, 1)
    assert len(giant) > csv.field_size_limit()
    bad = ledger.file("vancity/giant.csv", giant)
    good = ledger.file("vancity/export.csv", CSV_FIXTURE)

    out = ledger.derive()

    assert out.documents == {"parsed": 1, "unparsed": 1}
    assert out.lines.from_documents == 6
    statuses = {
        row.id: (row.status, row.reject_reason)
        for row in ledger.conn.execute(
            select(document.c.id, document.c.status, document.c.reject_reason)
        )
    }
    assert statuses[bad] == ("unparsed", "not_recognised")
    assert statuses[good] == ("parsed", None)
    assert (
        ledger.conn.scalar(
            select(func.count()).where(document_line.c.document_id == bad)
        )
        == 0
    )


def test_a_transaction_line_missing_a_field_is_counted_not_a_txn(
    ledger: Ledger,
) -> None:
    ledger.chequing()
    ledger.file("vancity/export.csv", CSV_FIXTURE)
    ledger.derive()
    ledger.conn.execute(delete(txn_source))
    ledger.conn.execute(delete(txn))
    ledger.conn.execute(
        update(document_line).where(document_line.c.line_no == 1).values(currency=None)
    )
    ledger.conn.execute(
        update(document_line).where(document_line.c.line_no == 2).values(amount=None)
    )
    ledger.conn.execute(
        update(document_line)
        .where(document_line.c.line_no == 3)
        .values(posting_date=None)
    )

    lines = build_lines(
        ledger.uow, CashLines((), ()), {}, zone=ledger.ctx.settings.timezone,
        overlap_days=3, receipt_days=3, invoice_days=30,
    )  # fmt: skip

    assert (lines.from_documents, lines.incomplete_lines) == (3, 3)
    assert count(ledger.conn, txn) == 3


def test_a_derive_that_fails_leaves_every_table_as_it_was(
    ledger: Ledger, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger.chequing()
    ledger.file("vancity/export.csv", CSV_FIXTURE)
    ledger.cash("paid-outside,2026-02-01,-12.00,CAD,coffee money,")
    ledger.derive()
    tables = [
        table
        for table in schema.metadata.tables.values()
        if not table.name.startswith("report_")
    ]

    def snapshot() -> dict[str, list[str]]:
        return {
            table.name: sorted(repr(tuple(row)) for row in ledger.conn.execute(
                select(table)
            ))
            for table in tables
        }  # fmt: skip

    before = snapshot()
    assert len(before["txn"]) == 7
    # The next derive would load other manual input, then fails in stage 3.
    ledger.cash("paid-outside,2026-02-02,-3.00,CAD,bus fare,")

    def failing(
        _uow: UnitOfWork,
        _cash: CashLines,
        _currencies: dict[str, set[str]],
        *,
        zone: ZoneInfo,
        overlap_days: int,
        receipt_days: int,
        invoice_days: int,
    ) -> Lines:
        raise RuntimeError("stage 3 failed")

    monkeypatch.setattr(stage3, "build_lines", failing)

    with pytest.raises(RuntimeError, match="stage 3 failed"):
        ledger.derive()

    assert snapshot() == before


def test_a_line_of_a_document_that_is_not_parsed_is_not_a_txn(ledger: Ledger) -> None:
    ledger.chequing()
    ledger.file("vancity/export.csv", CSV_FIXTURE)
    ledger.derive()
    ledger.conn.execute(update(document).values(status="superseded"))
    ledger.conn.execute(delete(txn_source))
    ledger.conn.execute(delete(txn))

    lines = build_lines(
        ledger.uow, CashLines((), ()), {}, zone=ledger.ctx.settings.timezone,
        overlap_days=3, receipt_days=3, invoice_days=30,
    )  # fmt: skip

    assert lines.txns == 0
    assert ledger.txns() == []


def test_a_violated_constraint_fails_the_derive_itself(
    ledger: Ledger, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger.chequing()
    real = stage3.build_lines

    def orphaning(
        uow: UnitOfWork,
        cash: CashLines,
        currencies: dict[str, set[str]],
        *,
        zone: ZoneInfo,
        overlap_days: int,
        receipt_days: int,
        invoice_days: int,
    ) -> Lines:
        lines = real(
            uow, cash, currencies, zone=zone, overlap_days=overlap_days,
            receipt_days=receipt_days, invoice_days=invoice_days,
        )  # fmt: skip
        ledger.conn.execute(
            insert(txn).values(
                id=derived_id(D(2026, 2, 1), ("orphan",)),
                account_id=account_id("chequing"),
                posting_date=D(2026, 2, 1),
                amount=Decimal("1.00"),
                currency="CAD",
                origin="manual",
                movement_kind="fiat",
            )
        )
        return lines

    monkeypatch.setattr(stage3, "build_lines", orphaning)

    with pytest.raises(IntegrityError, match="no primary source"):
        ledger.derive()


def test_a_cash_line_in_a_currency_its_account_does_not_take_is_reported(
    ledger: Ledger,
) -> None:
    ledger.chequing()
    ledger.cash(
        "paid-outside,2026-02-01,-12.00,CAD,coffee money,",
        "paid-outside,2026-02-02,-5.00,usd,sandwich,",
    )

    out = ledger.derive()

    assert (out.lines.from_manual, out.lines.manual_currency_refused) == (1, 1)
    assert [r["description_norm"] for r in ledger.txns()] == ["coffee money"]
    errors = ledger.conn.execute(
        select(manual_entry_error.c.file, manual_entry_error.c.line_no,
               manual_entry_error.c.reason)
    ).all()  # fmt: skip
    assert [tuple(e) for e in errors] == [("transactions.csv", 3, CURRENCY_REFUSED)]
    assert "USD" not in CURRENCY_REFUSED  # the reason names no value

    # The report is rebuilt, not accumulated.
    ledger.derive()
    assert count(ledger.conn, manual_entry_error) == 1


def test_the_manual_files_load_before_documents_are_parsed(ledger: Ledger) -> None:
    # A first derive: the account table is empty until stage 2 has run, and the parser
    # matches the document's account from it.
    ledger.chequing()
    ledger.file("vancity/export.csv", CSV_FIXTURE)
    assert count(ledger.conn, account) == 0

    out = ledger.derive()

    assert out.documents == {"parsed": 1}
    assert count(ledger.conn, txn) == 6
    assert ledger.conn.scalar(select(document.c.status)) == "parsed"


def test_a_document_whose_account_left_the_registry_is_parsed_again(
    ledger: Ledger,
) -> None:
    ledger.chequing()
    ledger.accounts(
        ("chequing", "Vancity", "chequing", "999000000077-999-99901-99902"),
        ("other", "Vancity", "chequing", "999000000055"),
    )
    doc = ledger.file("vancity/export.csv", CSV_FIXTURE)
    ledger.derive()
    assert count(ledger.conn, txn) == 6

    # The owner removes the account the export belongs to.
    ledger.accounts(("other", "Vancity", "chequing", "999000000055"))
    out = ledger.derive()

    assert out.documents == {"rejected": 1}
    assert out.lines.txns == 0
    assert count(ledger.conn, txn) == count(ledger.conn, txn_source) == 0
    assert count(ledger.conn, document_line) == 0
    assert count(ledger.conn, statement_period) == 0
    row = ledger.conn.execute(
        select(document.c.status, document.c.reject_reason).where(document.c.id == doc)
    ).one()
    assert tuple(row) == ("rejected", "unknown_account")
    assert (
        ledger.conn.scalar(
            select(func.count())
            .select_from(event)
            .where(event.c.type == DOCUMENT_REJECTED)
        )
        == 1
    )

    # Adding it back restores the same ids.
    ledger.accounts(
        ("chequing", "Vancity", "chequing", "999000000077-999-99901-99902"),
        ("other", "Vancity", "chequing", "999000000055"),
    )
    assert ledger.derive().lines.txns == 6


def test_state_tables_are_not_touched(
    ledger: Ledger, insert_decision: Callable[..., uuid.UUID]
) -> None:
    ledger.chequing()
    ledger.file("vancity/export.csv", CSV_FIXTURE)
    insert_decision(
        "finding_dismiss",
        at=dt.datetime(2026, 2, 1, tzinfo=dt.UTC),
        target_ref=f"finance.document:{uuid7(dt.datetime(2026, 2, 1, tzinfo=dt.UTC))}",
    )

    ledger.derive()

    assert count(ledger.conn, decision) == 1


def test_a_missing_archive_refuses_and_changes_nothing(ledger: Ledger) -> None:
    ledger.chequing()
    ledger.file("vancity/export.csv", CSV_FIXTURE)
    ledger.derive()
    before = ledger.txns()
    gone = dataclasses.replace(
        ledger.ctx,
        settings=parse_settings(
            f'[finance.paths]\narchive = "{ledger.archive}-gone"\n'
        ),
    )

    with pytest.raises(DocumentRefused):
        derive_run.run(ledger.uow, gone, extract_pdf=read_as_text)

    assert ledger.txns() == before


def test_the_sink_specs_are_rewritten_and_match_the_gate(
    ledger: Ledger,
    insert_decision: Callable[..., uuid.UUID],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ledger.chequing()

    def provider(uow: UnitOfWork) -> SinkSpec:
        dismissed = uow.scalar(
            select(func.count()).where(decision.c.kind == "finding_dismiss")
        )
        return SinkSpec(
            name="rates",
            hosts=("rates.example.test",),
            requests=(RequestTemplate(method="GET", path="/series/:code"),),
            key_file=None,
            payload_policy_version=1,
            data_inputs=(("dismissed", str(dismissed)),),
        )

    monkeypatch.setitem(SINK_SPECS, "rates", provider)
    insert_decision(
        "finding_dismiss",
        at=dt.datetime(2026, 2, 1, tzinfo=dt.UTC),
        target_ref=f"finance.document:{uuid7(dt.datetime(2026, 2, 1, tzinfo=dt.UTC))}",
    )

    ledger.derive()

    stored = ledger.conn.scalar(select(sink_spec.c.spec_digest))
    assert stored == spec_digest(provider(ledger.uow))
    assert count(ledger.conn, sink_spec) == 1


def test_every_table_is_cleared_rewritten_state_or_owned_by_the_file_operation() -> (
    None
):
    tables = {
        name
        for table in schema.metadata.tables.values()
        if not (name := table.name).startswith("report_")
    }
    groups = [
        {table.name for table in derive_run.CLEARED},
        set(derive_run.REWRITTEN),
        set(derive_run.STATE),
        set(derive_run.FILE_OWNED),
    ]
    assert sorted(name for group in groups for name in group) == sorted(tables)
