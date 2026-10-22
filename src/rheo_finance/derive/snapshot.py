"""The real snapshot provider: a line reference to what the line looks like now.

``txn_snapshot`` has the contract of ``context.SnapshotProvider`` and is what the
production context uses (``composition.build_context``). It resolves
``finance.txn:<uuid>`` through ``txn_source.line_key_id``, not through ``txn.id``, so a
reference taken on a provisional line still resolves after a statement line replaces it
(the old key stays on the txn as a source). A reference no current line carries gives
``None``, and so does anything that is not a well-formed ``finance.txn`` reference. An
evidence source (a receipt that matched a payment) is not a line of the payment and
does not resolve.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select

from rheo_finance.context import LineSnapshot, UnitOfWork
from rheo_finance.storage import refs
from rheo_finance.storage.schema import account, txn, txn_source

LINE_TYPE = "txn"


def txn_snapshot(uow: UnitOfWork, ref: str) -> LineSnapshot | None:
    try:
        parsed = refs.parse(ref)
    except refs.RefError:
        return None
    if parsed.system != refs.FINANCE_SYSTEM or parsed.type != LINE_TYPE:
        return None
    row = uow.execute(
        select(account.c.slug, txn.c.amount, txn.c.currency, txn.c.posting_date)
        .select_from(txn_source)
        .join(txn, txn.c.id == txn_source.c.txn_id)
        .join(account, account.c.id == txn.c.account_id)
        .where(
            txn_source.c.line_key_id == uuid.UUID(parsed.id),
            txn_source.c.role.in_(("primary", "additional")),
        )
    ).one_or_none()
    if row is None:
        return None
    return LineSnapshot(
        account_slug=row.slug,
        amount=row.amount,
        currency=row.currency,
        posting_date=row.posting_date,
    )
