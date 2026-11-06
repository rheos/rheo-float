"""Last-derive snapshot, not a promise of intraday eligibility freshness."""

from __future__ import annotations

import datetime as dt

from rheo_finance import queue, queue_store
from rheo_finance.context import FinanceContext, UnitOfWork


def refresh(ctx: FinanceContext, uow: UnitOfWork, *, as_of_date: dt.date) -> None:
    complete = queue.list_items(
        ctx, uow, queue.ListIn(counts_only=True), as_of_date=as_of_date
    )
    counts = {row.kind: row.count for row in complete.counts}
    if (
        tuple(counts) != queue.KINDS
        or len(complete.counts) != len(queue.KINDS)
        or sum(counts.values()) != complete.total
    ):
        raise ValueError("queue snapshot needs complete counts matching its total")
    queue_store.replace_counts(uow, counts, as_of_date=as_of_date)
