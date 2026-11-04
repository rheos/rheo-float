"""The event registry is a public-data boundary (FR-55, AC-56)."""

from typing import Annotated

from pydantic import BaseModel

from rheo_finance.contracts import EVENT_RESTRICTED_EXCEPTIONS, Tiered
from rheo_finance.tier import Tier

from .source_tree import event_registries
from .test_tiers import event_problems


def test_event_registry_has_no_restricted_fields() -> None:
    registry = event_registries()
    assert registry
    assert event_problems(registry, EVENT_RESTRICTED_EXCEPTIONS) == []


def test_planted_account_identifier_is_rejected() -> None:
    class Planted(BaseModel):
        account_number: Annotated[str, Tiered(Tier.PUBLIC)]

    assert event_problems({"finance.statement.became_available": Planted}, {})
