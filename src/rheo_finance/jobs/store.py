"""Typed job history persistence. Scheduling and retention policy live in tick."""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass

from sqlalchemy import delete, insert, select, text

from rheo_finance.context import UnitOfWork
from rheo_finance.storage.ids import uuid7
from rheo_finance.storage.schema import job_run


@dataclass(frozen=True)
class Run:
    id: uuid.UUID
    seq: int
    job: str
    started_at: dt.datetime
    finished_at: dt.datetime
    ok: bool | None
    detail_code: str
    input_digest: str | None


class PostgresJobRepository:
    def __init__(self, uow: UnitOfWork) -> None:
        self.uow = uow

    def all(self) -> tuple[Run, ...]:
        return tuple(
            Run(**dict(row))
            for row in self.uow.execute(
                select(job_run).order_by(job_run.c.finished_at, job_run.c.seq)
            ).mappings()
        )

    def add(
        self,
        *,
        job: str,
        started_at: dt.datetime,
        finished_at: dt.datetime,
        ok: bool | None,
        detail_code: str,
        input_digest: str | None,
    ) -> uuid.UUID:
        run_id = uuid7(started_at)
        self.uow.execute(
            insert(job_run).values(
                id=run_id,
                job=job,
                started_at=started_at,
                finished_at=finished_at,
                ok=ok,
                detail_code=detail_code,
                input_digest=input_digest,
            )
        )
        return run_id

    def add_legacy_tick_refusal(
        self, started: dt.datetime, finished: dt.datetime
    ) -> None:
        """0040 alone allocates an integer id; write only its common columns."""
        self.uow.execute(
            text(
                "INSERT INTO finance.job_run "
                "(job, started_at, finished_at, ok, detail_code) "
                "VALUES ('finance.tick', :started, :finished, false, 'failed')"
            ),
            {"started": started, "finished": finished},
        )

    def remove(self, ids: list[uuid.UUID]) -> None:
        if ids:
            self.uow.execute(delete(job_run).where(job_run.c.id.in_(ids)))
