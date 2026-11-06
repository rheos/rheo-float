"""Typed Current acknowledgement state. Transitions belong to the reconciler."""

from __future__ import annotations

import datetime as dt
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.engine import RowMapping

from rheo_finance.context import UnitOfWork
from rheo_finance.storage.schema import sink_ticket

Priority = Literal["normal", "high"]
Status = Literal["open", "done"]


class CreateSnapshot(BaseModel):
    """The safe first create, retained to recover even a vanished source."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)
    title: str = Field(min_length=1, max_length=500)
    body: str = Field(max_length=20000)
    due_on: dt.date
    priority: Priority


class SinkTicket(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)
    source_ref: str
    external_id: str | None = None
    state: Literal["post_pending", "open", "done"]
    create: CreateSnapshot
    last_title: str | None = None
    last_due_on: dt.date | None = None
    last_priority: Priority | None = None
    last_status: Status | None = None
    last_snoozed_until: dt.datetime | None = None
    settled_at: dt.datetime | None = None
    last_attempt_at: dt.datetime | None = None
    last_success_at: dt.datetime | None = None
    last_error_code: str | None = None


class CurrentTicketRepository(Protocol):
    def all(self) -> tuple[SinkTicket, ...]: ...

    def get(self, source_ref: str) -> SinkTicket | None: ...

    def insert_if_absent(self, ticket: SinkTicket) -> bool: ...

    def save(self, ticket: SinkTicket) -> None: ...


def _columns(ticket: SinkTicket) -> dict[str, object]:
    fields: dict[str, object] = ticket.model_dump(exclude={"create"})
    fields.update({f"create_{k}": v for k, v in ticket.create.model_dump().items()})
    return fields


def _ticket(columns: RowMapping) -> SinkTicket:
    fields = dict(columns)
    snapshot = {
        key: fields.pop(f"create_{key}")
        for key in ("title", "body", "due_on", "priority")
    }
    return SinkTicket.model_validate({**fields, "create": snapshot})


class PostgresCurrentTicketRepository:
    def __init__(self, uow: UnitOfWork) -> None:
        self.uow = uow

    def all(self) -> tuple[SinkTicket, ...]:
        return tuple(
            _ticket(row._mapping)
            for row in self.uow.execute(
                select(sink_ticket).order_by(sink_ticket.c.source_ref)
            )
        )

    def get(self, source_ref: str) -> SinkTicket | None:
        row = self.uow.execute(
            select(sink_ticket).where(sink_ticket.c.source_ref == source_ref)
        ).first()
        return None if row is None else _ticket(row._mapping)

    def insert_if_absent(self, ticket: SinkTicket) -> bool:
        found = self.uow.scalar(
            insert(sink_ticket)
            .values(**_columns(ticket))
            .on_conflict_do_nothing(index_elements=[sink_ticket.c.source_ref])
            .returning(sink_ticket.c.source_ref)
        )
        return found is not None

    def save(self, ticket: SinkTicket) -> None:
        self.uow.execute(
            update(sink_ticket)
            .where(sink_ticket.c.source_ref == ticket.source_ref)
            .values(**_columns(ticket))
        )
