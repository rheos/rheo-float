"""Tick/schema/status behavior on synthetic, disposable databases."""

from __future__ import annotations

import datetime as dt
import importlib
import uuid
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

import pytest
from alembic.operations import Operations
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import Connection, create_engine, insert, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.pool import NullPool

from conftest import DatabaseUnderTest
from jobs.test_tick import configured
from reminders.test_desired import add as add_reminder
from rheo_finance import catalogue, db, operations
from rheo_finance.cli import jobs as jobs_cli
from rheo_finance.cli import ledger as ledger_cli
from rheo_finance.cli.main import main
from rheo_finance.clock import FakeClock
from rheo_finance.context import FinanceContext
from rheo_finance.contracts import Role
from rheo_finance.jobs.ops import RecordIn
from rheo_finance.jobs.registry import JOBS, JobResult, JobSpec
from rheo_finance.jobs.tick import tick
from rheo_finance.reminders.desired import DesiredOut
from rheo_finance.reports.ops import StatusOut
from rheo_finance.settings import parse_settings
from rheo_finance.sinks.ops import SinkState, StateOut
from rheo_finance.storage.schema import job_run, queue_count, sink_ticket


def test_public_tick_records_heartbeat_even_when_idle(
    rolled_back_context: Callable[[Role], FinanceContext],
    tmp_path: Path,
    owner_conn: Connection,
) -> None:
    ctx = configured(rolled_back_context(Role.SERVICE), tmp_path)
    tick(ctx)
    assert tick(ctx).runs == ()
    rows = owner_conn.execute(
        select(job_run).where(job_run.c.job == "finance.tick")
    ).all()
    assert len(rows) == 2 and all(row.ok is True for row in rows)


def test_public_tick_behind_capable_schema_records_failure_and_runs_nothing(
    rolled_back_context: Callable[[Role], FinanceContext],
    tmp_path: Path,
    owner_conn: Connection,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    ctx = configured(rolled_back_context(Role.SERVICE), tmp_path)
    module = importlib.import_module("rheo_finance.jobs.tick")
    monkeypatch.setattr(module, "head_revision", lambda: "0099_future")
    for name in JOBS:

        def forbidden(_ctx: FinanceContext) -> JobResult:
            pytest.fail("schema refusal must run no callbacks")

        monkeypatch.setitem(JOBS, name, JobSpec(name, forbidden))
    monkeypatch.setattr(jobs_cli, "_context", lambda: ctx)
    assert main(["jobs", "tick"]) == 1
    error = capsys.readouterr().err
    assert "schema_behind: run finance db migrate" in error
    assert "Traceback" not in error
    row = owner_conn.execute(select(job_run)).one()
    assert row.job == "finance.tick" and row.ok is False
    assert row.detail_code == "schema_behind"
    assert (
        owner_conn.scalar(
            text("SELECT version_num FROM finance.alembic_version_finance")
        )
        == db.head_revision()
    )


@pytest.mark.parametrize(
    "revision",
    ["0040_job_run", "0041_job_identity", "0039_reader_boundaries", "unknown"],
)
def test_public_tick_refuses_true_legacy_shapes_without_implicit_migration(
    revision: str,
    scratch_database: DatabaseUnderTest,
    make_context: Callable[[Role], FinanceContext],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    catalogue.load_all()
    engine = create_engine(scratch_database.owner_url, poolclass=NullPool)
    chain = ScriptDirectory.from_config(db.alembic_config())
    old = chain.get_revision("0040_job_run")
    identity = chain.get_revision("0041_job_identity")
    assert old is not None and identity is not None
    try:
        with engine.connect() as connection:
            transaction = connection.begin()
            try:
                connection.execute(text("DROP VIEW finance.report_status"))
                connection.execute(text("DROP VIEW finance.report_job"))
                connection.execute(text("DROP TABLE finance.job_run"))
                if revision in {"0040_job_run", "0041_job_identity"}:
                    with Operations.context(MigrationContext.configure(connection)):
                        old.module.upgrade()
                        if revision == "0041_job_identity":
                            identity.module.upgrade()
                connection.execute(
                    text(
                        "UPDATE finance.alembic_version_finance "
                        "SET version_num=:revision"
                    ),
                    {"revision": revision},
                )
                ctx = configured(
                    operations.with_owner_connection(
                        make_context(Role.SERVICE), connection
                    ),
                    tmp_path,
                )
                for name in JOBS:

                    def forbidden(_ctx: FinanceContext) -> JobResult:
                        pytest.fail("legacy refusal must run no callbacks")

                    monkeypatch.setitem(JOBS, name, JobSpec(name, forbidden))
                reason = "schema_unknown" if revision == "unknown" else "schema_behind"
                with pytest.raises(operations.OperationError, match=reason):
                    tick(ctx)
                assert (
                    connection.scalar(
                        text("SELECT version_num FROM finance.alembic_version_finance")
                    )
                    == revision
                )
                if revision in {"0040_job_run", "0041_job_identity"}:
                    # 0040 has integer identity, so read fixed facts, not new metadata.
                    row = connection.execute(
                        text("SELECT job,ok,detail_code FROM finance.job_run")
                    ).one()
                    assert tuple(row) == ("finance.tick", False, "failed")
                else:
                    assert (
                        connection.scalar(text("SELECT to_regclass('finance.job_run')"))
                        is None
                    )
            finally:
                transaction.rollback()
    finally:
        engine.dispose()


def test_public_status_reader_tick_age_staleness_snapshots_and_schema(
    scratch_database: DatabaseUnderTest,
    make_context: Callable[[Role], FinanceContext],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    sink_settings: Callable[..., None],
) -> None:
    sink_settings()
    catalogue.load_all()
    engine = create_engine(scratch_database.owner_url, poolclass=NullPool)
    base = make_context(Role.OWNER)
    clock = FakeClock(dt.datetime(2026, 1, 15, 12, tzinfo=dt.UTC))
    ctx = operations.with_databases(
        replace(base, clock=clock),
        owner_url=make_url("postgresql+psycopg://invalid:invalid@127.0.0.1:1/unusable"),
        reader_url=scratch_database.reader_url,
    )
    monkeypatch.setattr(ledger_cli, "_context", lambda: ctx)

    def lines() -> dict[int, str]:
        assert main(["status", "--json"]) == 0
        out = StatusOut.model_validate_json(capsys.readouterr().out)
        assert len(out.lines) <= 15
        return {each.line_no: each.text for each in out.lines}

    try:
        with engine.begin() as connection:
            add_reminder(connection)
        owner = operations.with_databases(
            replace(
                base,
                settings=parse_settings(
                    '[finance.reminders]\nmin_due_date = "2026-01-01"\n'
                ),
            ),
            owner_url=scratch_database.owner_url,
            reader_url=scratch_database.reader_url,
        )
        desired = operations.dispatch(
            "finance.reminder.desired", {}, Role.OWNER, ctx=owner
        )
        assert isinstance(desired, DesiredOut) and len(desired.tickets) == 1
        with engine.connect() as connection:
            assert connection.execute(select(sink_ticket)).all() == []
        first = lines()
        assert first[2].endswith("no tick yet")
        assert "snapshot unavailable" in first[5] and "pairing 0" not in first[5]
        assert (
            first[6]
            == "reminders: 0 queued retries, pending total unavailable, current off"
        )
        with engine.begin() as connection:
            connection.execute(
                insert(job_run).values(
                    id=uuid.uuid4(),
                    job="finance.tick",
                    started_at=clock.now_utc(),
                    finished_at=clock.now_utc(),
                    ok=True,
                    detail_code="completed",
                )
            )
            for kind in (
                "pairing",
                "held_notice",
                "card_debit",
                "payment",
                "presumed_paid",
                "reattach",
                "category",
            ):
                connection.execute(
                    insert(queue_count).values(
                        kind=kind, count=0, as_of_date=dt.date(2026, 1, 14)
                    )
                )
        clock.advance(dt.timedelta(minutes=12))
        current = lines()
        assert current[2].endswith("tick 12 min ago")
        assert "last derive snapshot 2026-01-14" in current[5]
        assert "pairing 0" in current[5] and "presumed paid 0" in current[5]
        assert 9 not in current
        clock.advance(dt.timedelta(minutes=48))
        assert 9 not in lines(), "threshold is exceeded, not reached"
        clock.advance(dt.timedelta(seconds=1))
        assert "tick stale: last ran 1 hours ago" in lines()[9]
        # A date split is unavailable even if all seven count rows exist.
        with engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE finance.queue_count SET as_of_date='2026-01-13' "
                    "WHERE kind='pairing'"
                )
            )
        assert "snapshot unavailable" in lines()[5]
        with engine.begin() as connection:
            connection.execute(
                text("DELETE FROM finance.queue_count WHERE kind='pairing'")
            )
        assert "snapshot unavailable" in lines()[5]
        with engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE finance.alembic_version_finance "
                    "SET version_num='0041_job_identity'"
                )
            )
        behind = lines()
        assert (
            list(behind) == [1]
            and "schema behind code: run finance db migrate" in behind[1]
        )
        with engine.begin() as connection:
            connection.execute(
                text("UPDATE finance.alembic_version_finance SET version_num=:head"),
                {"head": db.head_revision()},
            )
        from rheo_finance.reports import ops as status_ops

        # Only effective sink state is injected; all reads still use reader DSN.
        enabled = SinkState(
            sink="current",
            setting_enabled=True,
            approval="current",
            approved_at=None,
            approved_spec_digest=None,
            spec_digest=None,
            enabled=True,
        )
        monkeypatch.setattr(
            status_ops, "sink_state", lambda *_: StateOut(sinks=[enabled])
        )
        assert "not synced since" not in lines()[6]
        with engine.begin() as connection:
            connection.execute(
                insert(job_run).values(
                    id=uuid.uuid4(),
                    job="finance.reminders_sync",
                    started_at=clock.now_utc(),
                    finished_at=clock.now_utc(),
                    ok=None,
                    detail_code="pending: current not configured",
                )
            )
        assert (
            "current not configured" in lines()[6]
            and "not synced since" not in lines()[6]
        )
        with engine.begin() as connection:
            connection.execute(
                insert(sink_ticket).values(
                    source_ref="finance:synthetic:bill-2026-01",
                    state="post_pending",
                    create_title="Synthetic reminder",
                    create_body="Synthetic reminder body",
                    create_due_on=dt.date(2026, 1, 15),
                    create_priority="normal",
                )
            )
            connection.execute(
                insert(job_run).values(
                    id=uuid.uuid4(),
                    job="finance.reminders_sync",
                    started_at=clock.now_utc(),
                    finished_at=clock.now_utc(),
                    ok=False,
                    detail_code="failed",
                )
            )
        assert lines()[6].startswith("reminders not synced since 2026-01-15 06:00 MST")
        assert "1 queued retries" in lines()[6]
        with engine.begin() as connection:
            connection.execute(
                insert(job_run).values(
                    id=uuid.uuid4(),
                    job="finance.reminders_sync",
                    started_at=clock.now_utc(),
                    finished_at=clock.now_utc(),
                    ok=True,
                    detail_code="completed",
                )
            )
        assert "last sync 2026-01-15 06:00 MST" in lines()[6]
        assert "not synced since" not in lines()[6]

    finally:
        # Scratch fixture owns complete database teardown before these commits.
        engine.dispose()


def test_schema_detail_is_reserved_for_failed_tick() -> None:
    from pydantic import ValidationError

    at = dt.datetime(2026, 1, 1, tzinfo=dt.UTC)
    with pytest.raises(ValidationError):
        RecordIn(
            job="finance.derive",
            started_at=at,
            finished_at=at,
            ok=False,
            detail_code="schema_behind",
        )


def test_public_tick_unknown_schema_requires_code_update_without_history_or_work(
    rolled_back_context: Callable[[Role], FinanceContext],
    tmp_path: Path,
    owner_conn: Connection,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    ctx = configured(rolled_back_context(Role.SERVICE), tmp_path)
    owner_conn.execute(
        text("UPDATE finance.alembic_version_finance SET version_num='unknown'")
    )
    for name in JOBS:

        def forbidden(_ctx: FinanceContext) -> JobResult:
            pytest.fail("unknown schema must run no callbacks")

        monkeypatch.setitem(JOBS, name, JobSpec(name, forbidden))
    monkeypatch.setattr(jobs_cli, "_context", lambda: ctx)
    assert main(["jobs", "tick"]) == 1
    error = capsys.readouterr().err
    assert "schema_unknown: update the code" in error
    assert "migrate" not in error and "Traceback" not in error
    assert owner_conn.execute(select(job_run)).all() == []
    assert (
        owner_conn.scalar(
            text("SELECT version_num FROM finance.alembic_version_finance")
        )
        == "unknown"
    )
