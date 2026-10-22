"""Every stored column has a tier (FR-46, AC-47a), and the shared ``MetaData`` mirrors
the migrated database, so a column a migration adds cannot dodge the check."""

from __future__ import annotations

from sqlalchemy import Connection, text

from rheo_finance import catalogue
from rheo_finance.db import VERSION_TABLE
from rheo_finance.storage.schema import metadata
from rheo_finance.storage.tiers import Tier, tier_of, untiered_columns


def test_every_metadata_column_has_a_tier() -> None:
    catalogue.load_all()  # every module that defines tables is loaded
    assert untiered_columns(metadata) == []


def test_the_metadata_mirrors_every_table_in_the_database(
    owner_conn: Connection,
) -> None:
    in_database = {
        (table, column)
        for table, column in owner_conn.execute(
            text(
                "SELECT table_name, column_name FROM information_schema.columns"
                " WHERE table_schema = 'finance' AND table_name <> :version"
            ),
            {"version": VERSION_TABLE},
        )
    }
    in_metadata = {
        (table.name, column.name)
        for table in metadata.tables.values()
        for column in table.columns
    }
    assert in_database == in_metadata


def test_money_and_notes_are_restricted() -> None:
    for table, column in [
        ("decision", "amount"),
        ("decision", "note"),
        ("decision_item", "amount"),
        ("decision_target", "amount"),
        ("decision_target", "merchant_key"),
    ]:
        assert tier_of(table, column) is Tier.RESTRICTED, (table, column)
