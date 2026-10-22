"""The tier of every stored column (FR-46, AC-47a).

Each module that defines a table in schema ``finance`` registers every one of its
columns here, in the same change that adds the table. Money, identifiers,
counterparties, memos and descriptions are ``Tier.RESTRICTED``. ``untiered_columns``
lists what is missing, and the guard test fails on any.
"""

from __future__ import annotations

from sqlalchemy import MetaData

from rheo_finance.tier import Tier

__all__ = ["Tier", "register_column", "tier_of", "untiered_columns"]

_COLUMN_TIERS: dict[tuple[str, str], Tier] = {}


def register_column(table: str, column: str, tier: Tier) -> None:
    """Declare ``table.column``'s tier; a second, different tier is an error."""
    if not isinstance(tier, Tier):
        raise TypeError(f"{table}.{column}: the tier must be a Tier, not {tier!r}")
    current = _COLUMN_TIERS.setdefault((table, column), tier)
    if current is not tier:
        raise ValueError(
            f"{table}.{column} is already {current.value}, not {tier.value}"
        )


def tier_of(table: str, column: str) -> Tier | None:
    """The declared tier of ``table.column``, or None when it has none."""
    return _COLUMN_TIERS.get((table, column))


def untiered_columns(metadata: MetaData) -> list[tuple[str, str]]:
    """Every ``(table, column)`` in ``metadata`` with no declared tier, sorted.

    Tables are named without their schema, as every finance table is in ``finance``.
    """
    return sorted(
        (table.name, column.name)
        for table in metadata.tables.values()
        for column in table.columns
        if (table.name, column.name) not in _COLUMN_TIERS
    )
