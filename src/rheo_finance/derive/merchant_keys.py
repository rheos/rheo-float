"""Stage 13's name-safe merchant keys and display labels.

Party attribution joins here when the peer-money feature provides its call site.
Printed name positions remain restricted; only owner-selected payee words label an
attributed e-Transfer. This stage does not run classification rules or a model.
"""

from __future__ import annotations

import re

from sqlalchemy import bindparam, select, update

from rheo_finance.context import UnitOfWork
from rheo_finance.manual.payees import Payees
from rheo_finance.reference.patterns import classify, etransfer_name
from rheo_finance.storage.schema import account, txn
from rheo_finance.text import compared, normalize_description

WITHHELD = "e-Transfer (name withheld)"


def _stripped(description: str, names: tuple[str, ...]) -> str:
    # Match complete folded token runs, including every repeated occurrence. Keep
    # the rest of the normalized description, including merchant punctuation.
    for name in sorted(names, key=len, reverse=True):
        words = compared(name).split()
        if words:
            pattern = r"(?<!\w)" + r"[\W_]+".join(map(re.escape, words)) + r"(?!\w)"
            description = re.sub(pattern, " ", description)
    return normalize_description(description).strip(" -:;,.()")


def build(uow: UnitOfWork, payees: Payees) -> None:
    """Assign each current txn a merchant unit and a safe display label."""
    patterns = tuple(pattern for payee in payees.payees for pattern in payee.patterns)
    changes: list[dict[str, object]] = []
    for row in uow.execute(
        select(
            txn.c.id,
            txn.c.description_norm,
            txn.c.counterparty,
            txn.c.amount,
            account.c.kind,
            account.c.institution,
        ).join(account, account.c.id == txn.c.account_id)
    ):
        description = normalize_description(row.description_norm or "")
        hints = classify(
            description,
            account_kind=row.kind,
            issuer=row.institution,
            credit=row.amount > 0,
        )
        if hints.etransfer_hint is not None:
            printed = etransfer_name(description, hints)
            payee = payees.naming(printed) if printed else None
            direction = "in" if row.amount > 0 else "out"
            key = (
                f"etransfer:{payee.slug}:{direction}"
                if payee
                else f"etransfer:{direction}:{row.id}"
            )
            label = payee.label if payee else WITHHELD
        else:
            names = (*patterns, row.counterparty) if row.counterparty else patterns
            key = _stripped(description, names)
            label = key.title() or "Transaction"
        changes.append({"b_id": row.id, "b_key": key, "b_label": label})
    if changes:
        uow.execute(
            update(txn)
            .where(txn.c.id == bindparam("b_id"))
            .values(
                merchant_key=bindparam("b_key"), display_label=bindparam("b_label")
            ),
            changes,
        )
