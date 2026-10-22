"""Column tiers: the registry and the helper that finds an untiered column."""

from __future__ import annotations

import pytest
from sqlalchemy import Column, Integer, MetaData, Numeric, Table, Text

from rheo_finance import tier
from rheo_finance.storage import tiers
from rheo_finance.tier import Tier


@pytest.fixture(autouse=True)
def fresh_registry(monkeypatch: pytest.MonkeyPatch) -> dict[tuple[str, str], Tier]:
    """An empty registry per test, so planted columns never reach the real one."""
    registry: dict[tuple[str, str], Tier] = {}
    monkeypatch.setattr(tiers, "_COLUMN_TIERS", registry)
    return registry


def test_tiers_reexports_the_one_tier_enum() -> None:
    assert tiers.Tier is tier.Tier


def test_planted_registrations_stay_in_the_test_registry(
    fresh_registry: dict[tuple[str, str], Tier],
) -> None:
    tiers.register_column("planted_tier_leak", "note", Tier.INTERNAL)
    assert fresh_registry == {("planted_tier_leak", "note"): Tier.INTERNAL}


def test_the_helper_reports_an_untiered_column() -> None:
    metadata = MetaData(schema="finance")
    Table(
        "planted_tier_probe",
        metadata,
        Column("id", Integer, primary_key=True),
        Column("amount", Numeric(20, 2)),
        Column("memo", Text),
    )
    tiers.register_column("planted_tier_probe", "id", tiers.Tier.PUBLIC)
    tiers.register_column("planted_tier_probe", "amount", tiers.Tier.RESTRICTED)

    assert tiers.untiered_columns(metadata) == [("planted_tier_probe", "memo")]

    tiers.register_column("planted_tier_probe", "memo", tiers.Tier.RESTRICTED)
    assert tiers.untiered_columns(metadata) == []
    assert tiers.tier_of("planted_tier_probe", "amount") is tiers.Tier.RESTRICTED
    assert tiers.tier_of("planted_tier_probe", "absent") is None


def test_a_column_keeps_one_tier() -> None:
    tiers.register_column("planted_tier_conflict", "note", tiers.Tier.INTERNAL)
    tiers.register_column("planted_tier_conflict", "note", tiers.Tier.INTERNAL)
    with pytest.raises(ValueError, match="already internal, not public"):
        tiers.register_column("planted_tier_conflict", "note", tiers.Tier.PUBLIC)
    with pytest.raises(TypeError):
        tiers.register_column("planted_tier_conflict", "other", "public")  # type: ignore[arg-type]
