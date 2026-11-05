"""Reader-only open items and summaries, both from the same filtered view rows."""

from __future__ import annotations

import datetime as dt
from collections import defaultdict
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from rheo_finance.context import FinanceContext, UnitOfWork
from rheo_finance.contracts import Role, SafetyClass, Tiered
from rheo_finance.operations import OperationSpec, register
from rheo_finance.storage.schema import report_open_item
from rheo_finance.tier import Tier

P, R = Tier.PUBLIC, Tier.RESTRICTED
Kind = Literal[
    "unresolved_transfer",
    "ambiguous_transfer",
    "unconverted",
    "needs_source",
    "unmatched_invoice",
    "orphaned_decision",
    "unapplied_credit",
]


class ListIn(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    year: int | None = Field(default=None, ge=1970, le=9999)
    kind: Kind | None = None


class OpenItemRow(BaseModel):
    model_config = ConfigDict(frozen=True)
    line_ref: Annotated[str, Tiered(P)]
    tax_year: Annotated[int, Tiered(P)]
    kind: Annotated[str, Tiered(P)]
    occurred_on: Annotated[dt.date, Tiered(P)]
    account_label: Annotated[str | None, Tiered(P)]
    direction: Annotated[Literal["in", "out"] | None, Tiered(P)]
    amount: Annotated[Decimal | None, Tiered(R)]
    currency: Annotated[str | None, Tiered(P)]
    cad_amount: Annotated[Decimal | None, Tiered(R)]
    state: Annotated[str, Tiered(P)]
    reason: Annotated[str, Tiered(P)]
    category: Annotated[str | None, Tiered(P)]
    tag: Annotated[str | None, Tiered(P)]
    candidate_count: Annotated[int, Tiered(P)]
    candidate_refs: Annotated[list[str], Tiered(P)]


class CurrencySums(BaseModel):
    model_config = ConfigDict(frozen=True)
    currency: Annotated[str, Tiered(P)]
    in_amount: Annotated[Decimal, Tiered(R)]
    out_amount: Annotated[Decimal, Tiered(R)]


class KindSummary(BaseModel):
    model_config = ConfigDict(frozen=True)
    kind: Annotated[str, Tiered(P)]
    count: Annotated[int, Tiered(P)]
    sums: Annotated[list[CurrencySums], Tiered(P)]


class ListOut(BaseModel):
    model_config = ConfigDict(frozen=True)
    items: Annotated[list[OpenItemRow], Tiered(P)]
    summary: Annotated[list[KindSummary], Tiered(P)]


def list_items(_ctx: FinanceContext, uow: UnitOfWork, data: ListIn) -> ListOut:
    query = select(report_open_item)
    if data.year is not None:
        query = query.where(report_open_item.c.tax_year == data.year)
    if data.kind is not None:
        query = query.where(report_open_item.c.kind == data.kind)
    rows = [
        OpenItemRow.model_validate(dict(row))
        for row in uow.execute(
            query.order_by(
                report_open_item.c.kind,
                report_open_item.c.occurred_on,
                report_open_item.c.line_ref,
            )
        ).mappings()
    ]
    grouped: dict[str, list[OpenItemRow]] = defaultdict(list)
    for row in rows:
        grouped[row.kind].append(row)
    summaries: list[KindSummary] = []
    for kind, items in grouped.items():
        amounts: dict[str, dict[str, Decimal]] = defaultdict(
            lambda: {"in": Decimal(0), "out": Decimal(0)}
        )
        for row in items:
            if (
                row.amount is not None
                and row.currency is not None
                and row.direction is not None
            ):
                amounts[row.currency][row.direction] += abs(row.amount)
        summaries.append(
            KindSummary(
                kind=kind,
                count=len(items),
                sums=[
                    CurrencySums(
                        currency=currency,
                        in_amount=totals["in"],
                        out_amount=totals["out"],
                    )
                    for currency, totals in sorted(amounts.items())
                ],
            )
        )
    return ListOut(items=rows, summary=summaries)


register(
    OperationSpec(
        name="finance.openitem.list",
        input_model=ListIn,
        output_model=ListOut,
        handler=list_items,
        safety_class=SafetyClass.READ,
        roles=frozenset(Role),
        connection="reader",
        tool_name="finance_open_items",
        description=(
            "Items outside totals, by year and kind, "
            "with counts and native-currency sums."
        ),
    )
)
