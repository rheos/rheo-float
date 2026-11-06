"""Line listing through the reader projection, with inclusive named filters."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Annotated, Self

from pydantic import BaseModel, ConfigDict, model_validator
from sqlalchemy import or_, select

from rheo_finance.context import FinanceContext, UnitOfWork
from rheo_finance.contracts import Role, SafetyClass, Tiered
from rheo_finance.operations import OperationSpec, register
from rheo_finance.storage.schema import report_txn
from rheo_finance.tier import Tier

P, R = Tier.PUBLIC, Tier.RESTRICTED


class ListIn(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    account: str | None = None
    date_from: dt.date | None = None
    date_to: dt.date | None = None
    state: str | None = None

    @model_validator(mode="after")
    def ordered_dates(self) -> Self:
        if self.date_from and self.date_to and self.date_from > self.date_to:
            raise ValueError("date_from must not follow date_to")
        return self


class Line(BaseModel):
    model_config = ConfigDict(frozen=True)
    line_ref: Annotated[str, Tiered(P)]
    account: Annotated[str, Tiered(P)]
    account_label: Annotated[str, Tiered(P)]
    posting_date: Annotated[dt.date, Tiered(P)]
    txn_date: Annotated[dt.date | None, Tiered(P)]
    occurred_at: Annotated[dt.datetime | None, Tiered(P)]
    recognition_date: Annotated[dt.date | None, Tiered(P)]
    amount: Annotated[Decimal, Tiered(R)]
    currency: Annotated[str, Tiered(P)]
    cad_amount: Annotated[Decimal | None, Tiered(R)]
    basis: Annotated[str | None, Tiered(P)]
    provisional: Annotated[bool, Tiered(P)]
    display_label: Annotated[str, Tiered(P)]
    category: Annotated[str | None, Tiered(P)]
    category_basis_class: Annotated[str | None, Tiered(P)]
    tag: Annotated[str | None, Tiered(P)]
    business_unit: Annotated[str | None, Tiered(P)]
    business_pct: Annotated[Decimal | None, Tiered(P)]
    tag_basis_class: Annotated[str | None, Tiered(P)]
    transfer_state: Annotated[str | None, Tiered(P)]
    pair_kind: Annotated[str | None, Tiered(P)]
    allocation_state: Annotated[str | None, Tiered(P)]
    exclusion: Annotated[str | None, Tiered(P)]
    decision_state: Annotated[str | None, Tiered(P)]
    split_state: Annotated[str | None, Tiered(P)]


class ListOut(BaseModel):
    model_config = ConfigDict(frozen=True)
    rows: Annotated[list[Line], Tiered(P)]


def list_lines(_ctx: FinanceContext, uow: UnitOfWork, data: ListIn) -> ListOut:
    c = report_txn.c
    query = select(report_txn)
    if data.account is not None:
        query = query.where(c.account == data.account)
    if data.date_from is not None:
        query = query.where(c.posting_date >= data.date_from)
    if data.date_to is not None:
        query = query.where(c.posting_date <= data.date_to)
    if data.state is not None:
        query = query.where(
            or_(
                *(
                    c[name] == data.state
                    for name in (
                        "transfer_state",
                        "pair_kind",
                        "allocation_state",
                        "exclusion",
                        "decision_state",
                        "split_state",
                    )
                )
            )
        )
    return ListOut(
        rows=[
            Line.model_validate(dict(row))
            for row in uow.execute(
                query.order_by(c.posting_date, c.line_ref)
            ).mappings()
        ]
    )


register(
    OperationSpec(
        name="finance.txn.list",
        input_model=ListIn,
        output_model=ListOut,
        handler=list_lines,
        safety_class=SafetyClass.READ,
        roles=frozenset(Role),
        connection="reader",
        tool_name="finance_lines",
        description="Safe ledger lines by account, posting date range and line state.",
    )
)
