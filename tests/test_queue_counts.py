"""Last-derive queue snapshot through real derive and a separate reader DSN."""

from __future__ import annotations

import dataclasses
import datetime as dt
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select, text, update
from sqlalchemy.pool import NullPool

from conftest import DatabaseUnderTest
from derive.support import CSV_IDENTIFIER, SECOND, THIRD, Ledger, read_as_text
from derive.test_payment_application import with_settings
from derive.test_payments import bank, notice, vendors
from derive.test_presumed_paid import history
from rheo_finance import catalogue, operations, queue
from rheo_finance.clock import FakeClock, local_today
from rheo_finance.context import FinanceContext, FinancePaths
from rheo_finance.contracts import Role
from rheo_finance.derive import queue_counts
from rheo_finance.derive.digest import ledger_digest
from rheo_finance.derive.run import run
from rheo_finance.derive.snapshot import txn_snapshot
from rheo_finance.settings import parse_settings
from rheo_finance.storage.schema import queue_count, report_queue_counts
from test_confirm import pending, pick, snapshot_matches, transfers

NOW = dt.datetime(2026, 2, 20, 12, tzinfo=dt.UTC)


@pytest.fixture
def scratch_ledger(
    scratch_database: DatabaseUnderTest, tmp_path: Path
) -> Iterator[Ledger]:
    catalogue.load_all()
    engine = create_engine(scratch_database.owner_url, poolclass=NullPool)
    with engine.connect() as conn:
        base = FinanceContext(
            settings=parse_settings(""),
            clock=FakeClock(NOW),
            paths=FinancePaths(data=tmp_path),
            role=Role.OWNER,
            snapshot_provider=txn_snapshot,
        )
        base = operations.with_databases(
            base,
            owner_url=scratch_database.owner_url,
            reader_url=scratch_database.reader_url,
        )
        ledger = Ledger(conn, operations.with_owner_connection(base, conn), tmp_path)
        ledger.accounts(
            ("chequing", "Vancity", "chequing", CSV_IDENTIFIER),
            ("second", "Vancity", "chequing", SECOND),
            ("third", "Vancity", "chequing", THIRD),
        )
        try:
            yield ledger
        finally:
            conn.rollback()
            assert (scratch_database.owner_url.database or "").startswith(
                "finance_scratch_"
            )
            conn.execute(
                text(
                    "TRUNCATE finance.queue_count, finance.account, "
                    "finance.document CASCADE"
                )
            )
            conn.commit()
    engine.dispose()


def reader_counts(database: DatabaseUnderTest) -> list[tuple[str, int, dt.date]]:
    engine = create_engine(database.reader_url, poolclass=NullPool)
    try:
        with engine.connect() as conn:
            assert conn.scalar(text("SELECT current_user")) == "finance_reader"
            return [
                (row.kind, row.count, row.as_of_date)
                for row in conn.execute(select(report_queue_counts)).mappings()
            ]
    finally:
        engine.dispose()


def test_unavailable_then_seven_zero_rows_and_content_digest(
    scratch_ledger: Ledger,
    scratch_database: DatabaseUnderTest,
) -> None:
    ledger = scratch_ledger
    assert reader_counts(scratch_database) == []
    ledger.derive()
    ledger.conn.commit()
    rows = reader_counts(scratch_database)
    assert {kind: count for kind, count, _ in rows} == dict.fromkeys(queue.KINDS, 0)
    day = local_today(ledger.ctx.clock, ledger.ctx.settings.timezone)
    assert {date for _, _, date in rows} == {day}
    digest = ledger_digest(ledger.uow).digest
    ledger.conn.execute(
        update(queue_count).values(as_of_date=day + dt.timedelta(days=1))
    )
    assert ledger_digest(ledger.uow).digest == digest
    ledger.conn.execute(
        update(queue_count).where(queue_count.c.kind == "category").values(count=1)
    )
    assert ledger_digest(ledger.uow).digest != digest
    ledger.conn.rollback()


def test_complete_counts_exceed_cap_then_direct_answer_is_a_documented_snapshot(
    scratch_ledger: Ledger,
    scratch_database: DatabaseUnderTest,
) -> None:
    ledger = scratch_ledger
    transfers(ledger, 15)
    ledger.conn.commit()
    rows = reader_counts(scratch_database)
    assert {kind: count for kind, count, _ in rows}["category"] == 15
    assert len(rows) == 7 and sum(count for _, count, _ in rows) == 15
    assert pending(ledger).batch_size == 10
    snapshot_matches(ledger)
    item = pick(ledger, "category")
    operations.dispatch(
        "finance.confirm.answer",
        {"code": item.code, "answer": "groceries"},
        Role.OWNER,
        ctx=ledger.ctx,
    )
    ledger.conn.commit()
    assert pending(ledger).total == 14
    assert reader_counts(scratch_database) == rows
    clock = ledger.ctx.clock
    assert isinstance(clock, FakeClock)
    clock.advance(dt.timedelta(days=1))
    ledger.ctx = dataclasses.replace(
        ledger.ctx,
        settings=parse_settings(
            f'[finance.paths]\narchive = "{ledger.archive}"\n'
            "[finance.classify]\nbatch_size = 1\n"
        ),
    )
    assert reader_counts(scratch_database) == rows
    ledger.derive()
    ledger.conn.commit()
    assert {kind: count for kind, count, _ in reader_counts(scratch_database)}[
        "category"
    ] == 14
    assert {day for _, _, day in reader_counts(scratch_database)} == {
        local_today(clock, ledger.ctx.settings.timezone)
    }
    snapshot_matches(ledger)
    digest = ledger_digest(ledger.uow).digest
    ledger.derive()
    assert ledger_digest(ledger.uow).digest == digest
    run(ledger.uow, ledger.ctx, reparse=True, extract_pdf=read_as_text)
    assert ledger_digest(ledger.uow).digest == digest


def test_once_with_sampled_date_and_late_failure_restores_complete_snapshot(
    scratch_ledger: Ledger,
    scratch_database: DatabaseUnderTest,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ledger = scratch_ledger
    transfers(ledger, 1)
    ledger.conn.commit()
    before = reader_counts(scratch_database)
    digest = ledger_digest(ledger.uow).digest
    calls: list[dt.date | None] = []
    original = queue.list_items

    def observed(
        ctx: FinanceContext,
        uow: object,
        data: queue.ListIn,
        *,
        as_of_date: dt.date | None = None,
    ) -> queue.ListOut:
        from rheo_finance.context import UnitOfWork

        assert isinstance(uow, UnitOfWork)
        assert data == queue.ListIn(counts_only=True)
        calls.append(as_of_date)
        return original(ctx, uow, data, as_of_date=as_of_date)

    monkeypatch.setattr(queue, "list_items", observed)
    ledger.derive()
    assert calls == [local_today(ledger.ctx.clock, ledger.ctx.settings.timezone)]
    original_refresh = queue_counts.refresh

    def late(ctx: FinanceContext, uow: object, *, as_of_date: dt.date) -> None:
        from rheo_finance.context import UnitOfWork

        assert isinstance(uow, UnitOfWork)
        original_refresh(ctx, uow, as_of_date=as_of_date)
        raise RuntimeError("synthetic late queue failure")

    monkeypatch.setattr(queue_counts, "refresh", late)
    ledger.cash("paid-outside,2026-02-02,-3.00,CAD,synthetic extra,")
    with pytest.raises(RuntimeError, match="synthetic late queue failure"):
        ledger.derive()
    assert ledger_digest(ledger.uow).digest == digest
    ledger.conn.commit()
    assert reader_counts(scratch_database) == before


def test_same_day_eligibility_setting_edit_leaves_last_derive_snapshot(
    scratch_ledger: Ledger,
    scratch_database: DatabaseUnderTest,
    notice_parser: None,
) -> None:
    ledger = scratch_ledger
    vendors(ledger)
    bank(ledger, (dt.date(2025, 6, 18), "SAMPLE ISP", Decimal("-201.00")))
    notice(ledger, dt.date(2025, 6, 10))
    with_settings(ledger, "[finance.payments]\nmatch_tolerance = 2.00\n")
    ledger.derive()
    ledger.conn.commit()
    before = reader_counts(scratch_database)
    assert dict((kind, count) for kind, count, _ in before)["held_notice"] == 1
    with_settings(ledger, "[finance.payments]\nmatch_tolerance = 0.01\n")
    live = pending(ledger, counts_only=True)
    assert {row.kind: row.count for row in live.counts}["held_notice"] == 0
    assert reader_counts(scratch_database) == before
    ledger.derive()
    ledger.conn.commit()
    snapshot_matches(ledger)
    assert {day for _, _, day in reader_counts(scratch_database)} == {
        day for _, _, day in before
    }


def test_sampled_date_controls_presumed_eligibility_without_new_operation_input(
    ledger: Ledger,
) -> None:
    history(ledger)
    ledger.derive()
    earlier = queue.list_items(
        ledger.ctx,
        ledger.uow,
        queue.ListIn(counts_only=True),
        as_of_date=dt.date(2022, 12, 1),
    )
    later = queue.list_items(
        ledger.ctx,
        ledger.uow,
        queue.ListIn(counts_only=True),
        as_of_date=dt.date(2026, 2, 20),
    )
    assert {row.kind: row.count for row in earlier.counts}["presumed_paid"] == 0
    assert {row.kind: row.count for row in later.counts}["presumed_paid"] > 0
    with pytest.raises(operations.InputInvalid):
        operations.dispatch(
            "finance.queue.list",
            {"as_of_date": "2023-01-01"},
            Role.OWNER,
            ctx=ledger.ctx,
        )
