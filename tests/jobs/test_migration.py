"""The frozen 0040 upgrade preserves populated run history and tie order."""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Callable
from pathlib import Path

import pytest
from alembic.operations import Operations
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, select, text
from sqlalchemy.pool import NullPool

from conftest import DatabaseUnderTest
from jobs.test_tick import configured
from rheo_finance import db, operations
from rheo_finance.context import FinanceContext
from rheo_finance.contracts import Role
from rheo_finance.jobs.registry import JOBS, JobResult, JobSpec
from rheo_finance.jobs.tick import tick
from rheo_finance.storage.schema import job_run, report_job


def test_populated_0040_upgrades_preserving_history_and_first_success(
    scratch_database: DatabaseUnderTest,
    make_context: Callable[[Role], FinanceContext],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = create_engine(scratch_database.owner_url, poolclass=NullPool)
    chain = ScriptDirectory.from_config(db.alembic_config())
    old = chain.get_revision("0040_job_run")
    upgrade = chain.get_revision("0041_job_identity")
    assert old is not None and upgrade is not None
    at = dt.datetime(2020, 1, 1, 12, tzinfo=dt.UTC)
    try:
        with engine.connect() as connection:
            transaction = connection.begin()
            try:
                # Only the disposable scratch fixture is touched. Recreate the
                # precise frozen 0040 shape in a rolled-back DDL transaction.
                connection.execute(text("DROP VIEW finance.report_job"))
                connection.execute(text("DROP TABLE finance.job_run"))
                context = MigrationContext.configure(connection)
                with Operations.context(context):
                    old.module.upgrade()
                for ok, detail in (
                    (True, "completed"),
                    (True, "completed"),
                    (None, "pending"),
                ):
                    connection.execute(
                        text(
                            "INSERT INTO finance.job_run(job,started_at,finished_at,"
                            "ok,detail_code,input_digest) VALUES "
                            "('finance.derive',:at,:at,:ok,:detail,:digest)"
                        ),
                        dict(at=at, ok=ok, detail=detail, digest="a" * 64),
                    )
                before = (
                    connection.execute(
                        text("SELECT * FROM finance.job_run ORDER BY id")
                    )
                    .mappings()
                    .all()
                )
                with Operations.context(context):
                    upgrade.module.upgrade()
                after = (
                    connection.execute(select(job_run).order_by(job_run.c.seq))
                    .mappings()
                    .all()
                )
                assert len(before) == len(after) == 3
                for previous, current in zip(before, after, strict=True):
                    assert current["seq"] == previous["id"]
                    for column in (
                        "job",
                        "started_at",
                        "finished_at",
                        "ok",
                        "detail_code",
                        "input_digest",
                    ):
                        assert current[column] == previous[column]
                    id_ = current["id"]
                    assert isinstance(id_, uuid.UUID) and id_.version == 7
                    assert id_.variant == uuid.RFC_4122
                    assert id_.int >> 80 == int(at.timestamp() * 1000)
                assert len({row["id"] for row in after}) == 3
                summary = connection.execute(select(report_job)).one()
                assert summary.last_success == at and summary.last_detail == "pending"
                ctx = operations.with_databases(
                    make_context(Role.SERVICE),
                    owner_url=scratch_database.owner_url,
                    reader_url=scratch_database.reader_url,
                )
                ctx = configured(
                    operations.with_owner_connection(ctx, connection), tmp_path
                )
                for job in JOBS:
                    monkeypatch.setitem(
                        JOBS, job, JobSpec(job, lambda c: JobResult(ok=True))
                    )
                tick(ctx)
                remaining = connection.scalars(select(job_run.c.id)).all()
                assert after[0]["id"] in remaining
                assert after[1]["id"] not in remaining
                assert after[2]["id"] not in remaining
            finally:
                transaction.rollback()
    finally:
        engine.dispose()
