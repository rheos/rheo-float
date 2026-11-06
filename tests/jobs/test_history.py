"""Persisted run history survives derives and retention without dirtying inputs."""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Callable
from pathlib import Path

import pytest
from sqlalchemy import Connection, select

from jobs.test_tick import configured
from rheo_finance import catalogue
from rheo_finance.context import FinanceContext, UnitOfWork
from rheo_finance.contracts import Role, SafetyClass
from rheo_finance.derive.digest import ledger_digest
from rheo_finance.derive.fingerprint import fingerprint
from rheo_finance.jobs.registry import JOBS, JobResult, JobSpec
from rheo_finance.jobs.tick import tick
from rheo_finance.operations import InputInvalid, RoleRefused, dispatch
from rheo_finance.storage.schema import job_run, report_job


def record(
    ctx: FinanceContext, *, job: str, at: dt.datetime, ok: bool | None = True
) -> uuid.UUID:
    out = dispatch(
        "finance.job.record",
        dict(
            job=job,
            started_at=at,
            finished_at=at,
            ok=ok,
            detail_code="completed" if ok else "failed" if ok is False else "pending",
        ),
        ctx.role,
        ctx=ctx,
    )
    result = out.model_dump()["id"]
    assert isinstance(result, uuid.UUID)
    return result


def test_first_success_of_every_job_survives_retention_with_timestamp_ties(
    rolled_back_context: Callable[[Role], FinanceContext],
    tmp_path: Path,
    owner_conn: Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    catalogue.load_all()
    ctx = configured(rolled_back_context(Role.SERVICE), tmp_path)
    old = ctx.clock.now_utc() - dt.timedelta(days=91)
    first: dict[str, uuid.UUID] = {}
    for job in JOBS:
        record(ctx, job=job, at=old, ok=False)
        first[job] = record(ctx, job=job, at=old)
        record(ctx, job=job, at=old)
        record(ctx, job=job, at=old, ok=None)
        monkeypatch.setitem(JOBS, job, JobSpec(job, lambda c: JobResult(ok=True)))
    boundary = record(ctx, job="finance.rates", at=old + dt.timedelta(days=1))
    tick(ctx)
    kept = set(owner_conn.scalars(select(job_run.c.id)))
    assert set(first.values()) <= kept and boundary in kept
    assert len(kept) == 9
    for job, id_ in first.items():
        assert (
            owner_conn.scalar(
                select(report_job.c.last_success).where(report_job.c.job == job)
            )
            == ctx.clock.now_utc()
        )
        assert id_ in kept


def test_job_records_are_output_state_unchanged_by_real_derive_and_rebuild(
    rolled_back_context: Callable[[Role], FinanceContext],
    tmp_path: Path,
    owner_conn: Connection,
) -> None:
    catalogue.load_all()
    ctx = configured(rolled_back_context(Role.SERVICE), tmp_path)
    dispatch("finance.ledger.derive", {}, ctx.role, ctx=ctx)
    uow = UnitOfWork(owner_conn)
    before = fingerprint(uow, ctx).digest, ledger_digest(uow).digest
    record(ctx, job="finance.derive", at=ctx.clock.now_utc())
    rows = owner_conn.execute(select(job_run)).all()
    assert (fingerprint(uow, ctx).digest, ledger_digest(uow).digest) == before
    for operation in ("finance.ledger.derive", "finance.ledger.rebuild"):
        dispatch(operation, {}, ctx.role, ctx=ctx)
        assert owner_conn.execute(select(job_run)).all() == rows
        assert (fingerprint(uow, ctx).digest, ledger_digest(uow).digest) == before


def test_record_catalogue_role_and_validation_do_not_persist_invalid_rows(
    rolled_back_context: Callable[[Role], FinanceContext],
    owner_conn: Connection,
) -> None:
    spec = catalogue.load_all()["finance.job.record"]
    assert spec.safety_class is SafetyClass.MUTATE and spec.connection == "owner"
    assert spec.roles == {Role.OWNER, Role.SERVICE} and spec.tool_name is None
    for role in (Role.OWNER, Role.SERVICE):
        ctx = rolled_back_context(role)
        record(ctx, job="finance.rates", at=ctx.clock.now_utc())
    ctx = rolled_back_context(Role.MEMBER)
    with pytest.raises(RoleRefused):
        record(ctx, job="finance.rates", at=ctx.clock.now_utc())
    ctx = rolled_back_context(Role.SERVICE)
    with pytest.raises(InputInvalid):
        dispatch(
            "finance.job.record",
            dict(
                job="finance.rates",
                started_at="2026-01-01",
                finished_at="2026-01-01",
                ok=True,
                detail_code="pending",
            ),
            ctx.role,
            ctx=ctx,
        )
    assert len(owner_conn.execute(select(job_run)).all()) == 2
