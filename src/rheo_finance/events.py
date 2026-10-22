"""Writing derived events (FR-53, FR-55; SPEC Data Models ``event``).

An event type is ``finance.<type>.<past-verb>`` and its ``data`` is a pydantic model
registered for that type in a module's ``EVENT_MODELS``, which holds identifiers and
small non-restricted facts only (the tier guard checks every registered model). The
event id derives from the type, the subject and any further key parts, so a rebuild
recreates the same events.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Mapping

from pydantic import BaseModel
from sqlalchemy.dialects.postgresql import insert

from rheo_finance.context import UnitOfWork
from rheo_finance.storage.ids import derived_id
from rheo_finance.storage.schema import event


def event_id(
    event_type: str,
    subject_ref: str,
    occurred_on: dt.date,
    key: tuple[object, ...] = (),
) -> uuid.UUID:
    return derived_id(occurred_on, ("event", event_type, subject_ref, *key))


def record_event(
    uow: UnitOfWork,
    models: Mapping[str, type[BaseModel]],
    event_type: str,
    subject_ref: str,
    occurred_on: dt.date,
    data: BaseModel,
    key: tuple[object, ...] = (),
) -> uuid.UUID:
    """Insert one event, or leave the one with its id in place. ``data`` must be an
    instance of the model ``models`` registers for ``event_type``."""
    model = models.get(event_type)
    if model is None:
        raise ValueError(f"{event_type} is not a registered event type")
    if type(data) is not model:
        raise TypeError(f"{event_type} data is a {model.__name__}")
    new_id = event_id(event_type, subject_ref, occurred_on, key)
    uow.execute(
        insert(event)
        .values(
            id=new_id,
            type=event_type,
            subject_ref=subject_ref,
            occurred_on=occurred_on,
            data=data.model_dump(mode="json"),
        )
        .on_conflict_do_nothing(index_elements=[event.c.id])
    )
    return new_id
