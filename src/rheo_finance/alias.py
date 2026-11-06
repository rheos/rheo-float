"""Owner-only, non-writing drafts from the shipped e-Transfer name policy.

Matching uses the stored payee patterns. Party attribution has no loader yet;
the owner chooses the party, slug and label in the printed draft slots.
"""

from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, ConfigDict

from rheo_finance.alias_store import AliasRepository, PostgresAliasRepository
from rheo_finance.context import FinanceContext, UnitOfWork
from rheo_finance.contracts import Role, SafetyClass, Tiered
from rheo_finance.manual.payees import names_any
from rheo_finance.operations import OperationSpec, register
from rheo_finance.reference.patterns import classify, etransfer_name
from rheo_finance.text import compared, normalize_description
from rheo_finance.tier import Tier


class DraftIn(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class DraftOut(BaseModel):
    model_config = ConfigDict(frozen=True)
    names: Annotated[list[str], Tiered(Tier.RESTRICTED)]


def draft_names(repository: AliasRepository) -> DraftOut:
    facts = repository.facts()
    names: dict[str, str] = {}
    for row in facts.lines:
        description = normalize_description(row.description_norm or "")
        hints = classify(
            description,
            account_kind=row.account_kind,
            issuer=row.institution,
            credit=row.amount > 0,
        )
        printed = etransfer_name(description, hints)
        if printed and not names_any(facts.patterns, printed):
            names.setdefault(compared(printed), printed)
    return DraftOut(names=[names[key] for key in sorted(names)])


def draft(_ctx: FinanceContext, uow: UnitOfWork, _data: DraftIn) -> DraftOut:
    return draft_names(PostgresAliasRepository(uow))


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
