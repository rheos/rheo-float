"""Owner-only, non-writing drafts from the shipped e-Transfer name policy.

Matching uses the stored payee patterns. Party attribution has no loader yet;
the owner chooses the party, slug and label in the printed draft slots.
"""

from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, ConfigDict
from sqlalchemy import select, text

from rheo_finance.context import FinanceContext, UnitOfWork
from rheo_finance.contracts import Role, SafetyClass, Tiered
from rheo_finance.db import READER_ROLE
from rheo_finance.manual.payees import names_any
from rheo_finance.operations import OperationSpec, RoleRefused, register
from rheo_finance.reference.patterns import classify, etransfer_name
from rheo_finance.storage.schema import account, payee, txn
from rheo_finance.text import compared, normalize_description
from rheo_finance.tier import Tier


class DraftIn(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class DraftOut(BaseModel):
    model_config = ConfigDict(frozen=True)
    names: Annotated[list[str], Tiered(Tier.RESTRICTED)]


def draft(_ctx: FinanceContext, uow: UnitOfWork, _data: DraftIn) -> DraftOut:
    # Check the actual connection, even when the caller claims the owner role.
    if uow.scalar(text("SELECT current_user")) == READER_ROLE:
        raise RoleRefused("finance.alias.draft refuses the reader connection")
    patterns = tuple(
        pattern
        for row in uow.execute(select(payee.c.patterns))
        for pattern in row.patterns
    )
    names: dict[str, str] = {}
    for row in uow.execute(
        select(
            txn.c.description_norm, txn.c.amount, account.c.kind, account.c.institution
        ).join(account, account.c.id == txn.c.account_id)
    ):
        description = normalize_description(row.description_norm or "")
        hints = classify(
            description,
            account_kind=row.kind,
            issuer=row.institution,
            credit=row.amount > 0,
        )
        printed = etransfer_name(description, hints)
        if printed and not names_any(patterns, printed):
            names.setdefault(compared(printed), printed)
    return DraftOut(names=[names[key] for key in sorted(names)])


register(
    OperationSpec(
        name="finance.alias.draft",
        input_model=DraftIn,
        output_model=DraftOut,
        handler=draft,
        safety_class=SafetyClass.DRAFT,
        roles=frozenset({Role.OWNER}),
        connection="owner",
        owner_only=True,
        description=(
            "Unmatched e-Transfer names for manual alias and payee drafts;"
            " writes nothing."
        ),
    )
)
