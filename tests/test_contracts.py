"""The contract enums and the ``Tiered`` output-field marker."""

from __future__ import annotations

from decimal import Decimal
from typing import Annotated

import pytest
from pydantic import BaseModel

from rheo_finance.contracts import Role, SafetyClass, Tiered, field_tiers
from rheo_finance.tier import Tier


def test_safety_class_has_exactly_read_draft_mutate() -> None:
    assert [member.value for member in SafetyClass] == ["read", "draft", "mutate"]


def test_role_has_exactly_owner_member_service() -> None:
    assert [member.value for member in Role] == ["owner", "member", "service"]


class _Output(BaseModel):
    count: Annotated[int, Tiered(Tier.PUBLIC)]
    amount: Annotated[Decimal, Tiered(Tier.RESTRICTED)]
    note: str


def test_field_tiers_reads_the_marker_and_finds_an_unmarked_field() -> None:
    assert field_tiers(_Output) == {
        "count": Tier.PUBLIC,
        "amount": Tier.RESTRICTED,
        "note": None,
    }
    assert _Output(count=1, amount=Decimal("9.99"), note="x").amount == Decimal("9.99")


def test_tiered_takes_a_tier() -> None:
    with pytest.raises(TypeError):
        Tiered("public")  # type: ignore[arg-type]
