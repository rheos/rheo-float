"""The service/owner job-recording operation, separate from callback transactions."""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Annotated, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from rheo_finance.clock import as_utc
from rheo_finance.context import FinanceContext, UnitOfWork
from rheo_finance.contracts import Role, SafetyClass, Tiered
from rheo_finance.jobs.registry import JOBS, TICK_JOB, Detail
from rheo_finance.jobs.store import PostgresJobRepository
from rheo_finance.operations import OperationError, OperationSpec, register
from rheo_finance.tier import Tier


class RecordIn(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    job: str
    started_at: dt.datetime
    finished_at: dt.datetime
    ok: bool | None
    detail_code: Detail
    input_digest: Annotated[str | None, Field(pattern=r"^[a-f0-9]{64}$")] = None

    @field_validator("started_at", "finished_at")
    @classmethod
    def aware(cls, value: dt.datetime) -> dt.datetime:
        return as_utc(value)

    @model_validator(mode="after")
    def valid(self) -> Self:
        if self.detail_code == "schema_behind" and self.job != TICK_JOB:
            raise ValueError("schema refusal belongs to the tick")
        if self.finished_at < self.started_at:
            raise ValueError("finish precedes start")
        if (
            (self.ok is True and self.detail_code != "completed")
            or (
                self.ok is False and self.detail_code not in {"failed", "schema_behind"}
            )
            or (
                self.ok is None
                and self.detail_code in {"completed", "failed", "schema_behind"}
            )
        ):
            raise ValueError("detail does not match outcome")
        return self


class RecordOut(BaseModel):
    model_config = ConfigDict(frozen=True)
    id: Annotated[uuid.UUID, Tiered(Tier.PUBLIC)]


def record(_ctx: FinanceContext, uow: UnitOfWork, data: RecordIn) -> RecordOut:
    if data.job not in JOBS and data.job != TICK_JOB:
        raise OperationError("job is not registered")
    return RecordOut(id=PostgresJobRepository(uow).add(**data.model_dump()))


register(
    OperationSpec(
        name="finance.job.record",
        input_model=RecordIn,
        output_model=RecordOut,
        handler=record,
        safety_class=SafetyClass.MUTATE,
        roles=frozenset({Role.OWNER, Role.SERVICE}),
        connection="owner",
        description="Record a completed job attempt using a fixed local outcome code.",
    )
)
