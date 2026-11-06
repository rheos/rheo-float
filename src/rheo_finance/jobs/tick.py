"""One catch-up run per due job; derive additionally checks current inputs.

No transaction surrounds callbacks. Each dispatched operation commits before the
next callback, and Current owns its per-ticket transactions. Skips/pending have
no success anchor, but suppress duplicate work for the same scheduled moment;
failures remain eligible for retry. No attempt is claimed before callback work.
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass

from sqlalchemy import text

from rheo_finance import catalogue
from rheo_finance.clock import as_utc, local_to_utc
from rheo_finance.context import FinanceContext
from rheo_finance.contracts import Role
from rheo_finance.db import VERSION_TABLE, head_revision
from rheo_finance.derive.fingerprint import fingerprint
from rheo_finance.jobs.ops import RecordIn
from rheo_finance.jobs.registry import JOBS, JobResult
from rheo_finance.jobs.store import PostgresJobRepository, Run
from rheo_finance.operations import (
    OperationError,
    RoleRefused,
    dispatch,
    owner_transaction,
)
from rheo_finance.sinks.gate import SinkDisabled


@dataclass(frozen=True)
class JobRun:
    job: str
    result: JobResult


@dataclass(frozen=True)
class TickResult:
    as_of: dt.datetime
    runs: tuple[JobRun, ...]


def latest_scheduled(ctx: FinanceContext, now: dt.datetime) -> dt.datetime:
    """Latest local interval boundary, compared/stored as a UTC instant."""
    minutes = ctx.settings.get_int("finance.jobs.tick_minutes")
    if not 1 <= minutes <= 1440:
        raise OperationError("job interval must be between 1 and 1440 minutes")
    local = now.astimezone(ctx.settings.timezone)
    passed = local.hour * 60 + local.minute
    slot = (passed // minutes) * minutes
    return local_to_utc(
        local.date(), dt.time(slot // 60, slot % 60), ctx.settings.timezone
    )


def _due(rows: tuple[Run, ...], job: str, scheduled: dt.datetime) -> bool:
    successful = [row for row in rows if row.job == job and row.ok is True]
    if successful and successful[-1].finished_at >= scheduled:
        return False
    previous = [row for row in rows if row.job == job]
    return not (
        previous and previous[-1].ok is None and previous[-1].finished_at >= scheduled
    )


def _prune(ctx: FinanceContext, now: dt.datetime) -> None:
    with owner_transaction(ctx) as uow:
        repo = PostgresJobRepository(uow)
        rows = repo.all()
        first: dict[str, uuid.UUID] = {}
        for row in rows:
            if row.ok is True:
                first.setdefault(row.job, row.id)
        cutoff = now - dt.timedelta(days=90)
        repo.remove(
            [
                row.id
                for row in rows
                if row.finished_at < cutoff and first.get(row.job) != row.id
            ]
        )


def tick(ctx: FinanceContext) -> TickResult:
    """Run due jobs serially as SERVICE. No job may borrow owner authorization."""
    if ctx.role is not Role.SERVICE:
        raise RoleRefused("jobs tick requires service role")
    catalogue.load_all()
    now = as_utc(ctx.clock.now_utc())
    scheduled = latest_scheduled(ctx, now)
    with owner_transaction(ctx) as uow:
        version = uow.scalar(text(f"SELECT version_num FROM finance.{VERSION_TABLE}"))
        if version != head_revision():
            raise OperationError("schema_behind")
        rows = PostgresJobRepository(uow).all()
    completed: list[JobRun] = []
    ledger_ready = True
    for name, spec in JOBS.items():
        if name == "finance.reminders_sync" and not ledger_ready:
            continue
        if name == "finance.derive":
            if not ledger_ready:
                continue
            # Filing has committed before this fingerprint is sampled.
            with owner_transaction(ctx) as uow:
                taken = fingerprint(uow, ctx).digest
            successes = [r for r in rows if r.job == name and r.ok is True]
            if successes and successes[-1].input_digest == taken:
                continue
        elif not _due(rows, name, scheduled):
            continue
        started = as_utc(ctx.clock.now_utc())
        try:
            result = spec.callback(ctx)
        except SinkDisabled:
            result = JobResult(ok=None, detail_code="skipped: sink off")
        except Exception:
            # Never persist exception text: it may contain paths, SQL or payloads.
            result = JobResult(ok=False, detail_code="failed")
        dispatch(
            "finance.job.record",
            RecordIn(
                job=name,
                started_at=started,
                finished_at=as_utc(ctx.clock.now_utc()),
                ok=result.ok,
                detail_code=result.detail_code,
                input_digest=result.input_digest,
            ),
            Role.SERVICE,
            ctx=ctx,
        )
        completed.append(JobRun(job=name, result=result))
        if (
            name in {"finance.file_downloads", "finance.derive"}
            and result.ok is not True
        ):
            ledger_ready = False
    _prune(ctx, now)
    return TickResult(as_of=now, runs=tuple(completed))
