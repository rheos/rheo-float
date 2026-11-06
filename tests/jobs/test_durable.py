"""Real commits are confined to the existing disposable scratch fixture."""

from __future__ import annotations

import csv
import datetime as dt
from collections.abc import Callable
from pathlib import Path

import pytest
from pydantic import BaseModel, ConfigDict
from sqlalchemy import Engine, create_engine, delete, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.pool import NullPool

from conftest import DatabaseUnderTest
from jobs.test_tick import configured
from rheo_finance import catalogue, operations
from rheo_finance.context import FinanceContext, UnitOfWork
from rheo_finance.contracts import Role, SafetyClass
from rheo_finance.jobs.ops import RecordIn, RecordOut
from rheo_finance.jobs.tick import tick
from rheo_finance.manual.headers import MANUAL_FILES
from rheo_finance.operations import OperationError, OperationSpec, dispatch
from rheo_finance.storage.schema import job_run, obligation, report_job, sink_ticket
from sinks.support import CurrentStub


def test_current_commits_before_job_record_failure_and_derive_is_already_visible(
    durable_current_context: FinanceContext,
    scratch_current_engine: Engine,
    current_stub: CurrentStub,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    catalogue.load_all()
    ctx = configured(durable_current_context, tmp_path)
    manual = ctx.paths.data / "manual"
    manual.mkdir(exist_ok=True)
    rows = {
        "payees.csv": dict(
            slug="sample-utility", label="Sample Utility", kind="vendor"
        ),
        "obligations.csv": dict(
            payee="sample-utility",
            kind="bill",
            amount_due="40.00",
            due_date="2026-05-20",
            issued_on="2026-05-01",
        ),
    }
    for name, values in rows.items():
        with (manual / name).open("w") as stream:
            writer = csv.DictWriter(stream, fieldnames=MANUAL_FILES[name])
            writer.writeheader()
            writer.writerow(values)
    observations: list[bool] = []

    def before_request() -> None:
        with scratch_current_engine.connect() as connection:
            assert connection.scalar(select(obligation.c.id)) is not None
            jobs = connection.scalars(select(job_run.c.job)).all()
            assert "finance.derive" in jobs and "finance.file_downloads" in jobs
            assert "finance.reminders_sync" not in jobs
            observations.append(True)

    current_stub.before_create = before_request
    original = operations.SPECS["finance.job.record"]

    def fail_record(c: FinanceContext, u: UnitOfWork, data: RecordIn) -> RecordOut:
        if data.job == "finance.reminders_sync":
            raise OperationError("synthetic record failure")
        result = original.handler(c, u, data)
        assert isinstance(result, RecordOut)
        return result

    monkeypatch.setitem(
        operations.SPECS,
        original.name,
        original.model_copy(update={"handler": fail_record}),
    )
    with pytest.raises(OperationError, match="synthetic record failure"):
        tick(ctx)
    with scratch_current_engine.connect() as connection:
        row = connection.execute(select(sink_ticket)).one()
        assert row.state == "open" and row.last_success_at is not None
        assert row.external_id is not None and observations
        assert (
            connection.scalar(
                select(job_run.c.id).where(job_run.c.job == "finance.reminders_sync")
            )
            is None
        )
    # Register cleanup before any test commit: scratch_current_engine already did
    # so for delivery rows; the scratch DB teardown covers the job history too.
    with scratch_current_engine.begin() as connection:
        connection.execute(delete(job_run))


class Empty(BaseModel):
    model_config = ConfigDict(frozen=True)


def test_report_job_reads_committed_history_on_reader_with_invalid_owner_dsn(
    scratch_database: DatabaseUnderTest,
    make_context: Callable[[Role], FinanceContext],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    catalogue.load_all()
    ctx = operations.with_databases(
        make_context(Role.SERVICE),
        owner_url=scratch_database.owner_url,
        reader_url=scratch_database.reader_url,
    )
    at = ctx.clock.now_utc()
    dispatch(
        "finance.job.record",
        RecordIn(
            job="finance.rates",
            started_at=at,
            finished_at=at,
            ok=True,
            detail_code="completed",
        ),
        Role.SERVICE,
        ctx=ctx,
    )
    next_day = at + dt.timedelta(days=1)
    dispatch(
        "finance.job.record",
        RecordIn(
            job="finance.rates",
            started_at=next_day,
            finished_at=next_day,
            ok=False,
            detail_code="failed",
        ),
        Role.SERVICE,
        ctx=ctx,
    )
    dispatch(
        "finance.job.record",
        RecordIn(
            job="finance.rates",
            started_at=next_day,
            finished_at=next_day,
            ok=None,
            detail_code="skipped: sink off",
        ),
        Role.SERVICE,
        ctx=ctx,
    )

    def read(_ctx: FinanceContext, uow: UnitOfWork, _data: Empty) -> Empty:
        row = uow.execute(
            select(report_job).where(report_job.c.job == "finance.rates")
        ).one()
        assert row.last_success == at and row.last_detail == "skipped: sink off"
        assert uow.scalar(text("SELECT current_user")) == "finance_reader"
        return Empty()

    name = "finance.fixture.jobs"
    monkeypatch.setitem(
        operations.SPECS,
        name,
        OperationSpec(
            name=name,
            input_model=Empty,
            output_model=Empty,
            handler=read,
            safety_class=SafetyClass.READ,
            roles=frozenset({Role.MEMBER}),
            connection="reader",
            description="Synthetic view boundary probe.",
        ),
    )
    reader_ctx = operations.with_databases(
        make_context(Role.MEMBER),
        owner_url=scratch_database.owner_url.set(port=1),
        reader_url=scratch_database.reader_url,
    )
    assert isinstance(dispatch(name, {}, Role.MEMBER, ctx=reader_ctx), Empty)
    engine = create_engine(scratch_database.reader_url, poolclass=NullPool)
    try:
        for sql in (
            "SELECT * FROM finance.job_run",
            "DELETE FROM finance.job_run",
            "UPDATE finance.report_job SET last_detail = 'failed'",
            "INSERT INTO finance.job_run(job) VALUES ('finance.rates')",
        ):
            with engine.connect() as connection:
                with pytest.raises(DBAPIError):
                    connection.execute(text(sql))
                connection.rollback()
    finally:
        engine.dispose()


def test_reader_identity_default_probe_requires_real_permission_denial(
    scratch_database: DatabaseUnderTest,
) -> None:
    import psycopg
    # Permission probe against the generated run sequence.
    engine = create_engine(scratch_database.reader_url, poolclass=NullPool)
    try:
        with engine.connect() as connection:
            with pytest.raises(DBAPIError) as caught:
                connection.execute(text("UPDATE finance.job_run SET seq = DEFAULT"))
            connection.rollback()
            assert isinstance(caught.value.orig, psycopg.errors.InsufficientPrivilege)
    finally:
        engine.dispose()
