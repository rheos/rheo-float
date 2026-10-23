"""Derivation stage 5: recognition (FR-16).

Spending and income are recognized on the date they happened on the account where they
happened: the card transaction date on a ``credit_card`` account, the posting date
everywhere else. A card line with no transaction date falls back to its posting date.
A card payment is a transfer, so a purchase is never recognized on the date the card is
paid. ``txn.tax_year`` is a generated column, the calendar year of the recognition date
(0010), so setting the date sets the year.

A crypto line's posting date is already its local date in ``finance.locale.timezone``
(stage 3), so it needs nothing more here.
"""

from __future__ import annotations

from sqlalchemy import case, func, update

from rheo_finance.context import UnitOfWork
from rheo_finance.storage.schema import account, txn

CARD_KIND = "credit_card"


def recognize(uow: UnitOfWork) -> None:
    """Set every txn's recognition date from its account's kind."""
    uow.execute(
        update(txn)
        .where(account.c.id == txn.c.account_id)
        .values(
            recognition_date=case(
                (
                    account.c.kind == CARD_KIND,
                    func.coalesce(txn.c.txn_date, txn.c.posting_date),
                ),
                else_=txn.c.posting_date,
            )
        )
    )
