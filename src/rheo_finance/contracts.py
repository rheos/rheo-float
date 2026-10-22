"""Shared operation contracts: safety class, caller role, output-field tier, and the
event fields allowed to be restricted.

``SafetyClass`` and ``Role`` mirror rheoStream's enums. This module declares only the
three classes its operations use: there is no FINANCIAL and no EXTERNAL class.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Final

from pydantic import BaseModel

from rheo_finance.tier import Tier


class SafetyClass(StrEnum):
    READ = "read"
    DRAFT = "draft"
    MUTATE = "mutate"


class Role(StrEnum):
    OWNER = "owner"
    MEMBER = "member"
    SERVICE = "service"


@dataclass(frozen=True, slots=True)
class Tiered:
    """An operation output field's tier.

    Use it as ``Annotated[Decimal, Tiered(Tier.RESTRICTED)]``.
    """

    tier: Tier

    def __post_init__(self) -> None:
        if not isinstance(self.tier, Tier):
            raise TypeError(f"Tiered takes a Tier, not {self.tier!r}")


def field_tiers(model: type[BaseModel]) -> dict[str, Tier | None]:
    """Each field of ``model`` with the tier its ``Tiered`` marker gives, or None."""
    tiers: dict[str, Tier | None] = {}
    for name, info in model.model_fields.items():
        marks = [mark.tier for mark in info.metadata if isinstance(mark, Tiered)]
        if len(marks) > 1:
            raise ValueError(f"{model.__name__}.{name} has more than one Tiered marker")
        tiers[name] = marks[0] if marks else None
    return tiers


# Event data holds no restricted field (FR-55, AC-47a, AC-56), except the fields listed
# here for their event type: ``{event type: field names}``. Each entry is reviewed in
# the change that adds it; the guard test fails on a restricted event field not listed
# here and on an entry naming no restricted field.
EVENT_RESTRICTED_EXCEPTIONS: Final[Mapping[str, frozenset[str]]] = MappingProxyType({})
