"""The reader role's whole world is the ``report_*`` views (FR-48, AC-49a, AC-53, Key
decision 21).

Seams: the role's privileges as the migrated ``finance_test`` catalog grants them, the
reader DSN refusing every write and every DDL statement, and every reader-connection
operation in the catalogue dispatched under the reader DSN. ``test_db`` probes each
base table one statement at a time; here the catalog is read whole, so a grant added
anywhere shows.
"""

from __future__ import annotations

from collections.abc import Callable

import psycopg
import pytest
from sqlalchemy import Connection, Engine, text
from sqlalchemy.exc import DBAPIError

from guards.test_tiers import restricted_by_name
from rheo_finance import catalogue
from rheo_finance.context import FinanceContext
from rheo_finance.contracts import Role
from rheo_finance.operations import dispatch
from rheo_finance.storage.schema import metadata
from rheo_finance.storage.tiers import tier_of
from rheo_finance.tier import Tier

MakeContext = Callable[[Role], FinanceContext]
READER = "finance_reader"
PRIVILEGES = (
    "SELECT",
    "INSERT",
    "UPDATE",
    "DELETE",
    "TRUNCATE",
    "REFERENCES",
    "TRIGGER",
)
# The views this build defines; a later prompt that adds one adds it here.
REPORT_VIEWS = frozenset(
    {
        "report_sink_spec",
        "report_sink",
        "report_status",
        "report_coverage",
        "report_document",
    }
)
# Inputs for reader operations whose input model has required fields; {} otherwise.
READER_INPUTS: dict[str, dict[str, object]] = {}
# Column names no report view may carry, beyond the restricted-name shapes.
NEVER_SHOWN = frozenset(
    {
        "description_raw",
        "description_norm",
        "merchant_key",
        "counterparty",
        "identifier",
    }
)


def relations(conn: Connection) -> dict[str, str]:
    """Every relation in schema ``finance``, name to relkind."""
    rows = conn.execute(
        text(
            "SELECT c.relname, c.relkind FROM pg_class c"
            " JOIN pg_namespace n ON n.oid = c.relnamespace"
            " WHERE n.nspname = 'finance'"
            " AND c.relkind IN ('r', 'p', 'v', 'm', 'S', 'f')"
        )
    )
    return {name: kind for name, kind in rows}


def granted(conn: Connection, relation: str) -> set[str]:
    """The privileges the reader holds on ``finance.<relation>``."""
    return {
        privilege
        for privilege in PRIVILEGES
        if conn.execute(
            text("SELECT has_table_privilege(:role, :relation, :privilege)"),
            {
                "role": READER,
                "relation": f'finance."{relation}"',
                "privilege": privilege,
            },
        ).scalar_one()
    }


def test_the_report_views_are_views_owned_by_finance_owner(
    owner_conn: Connection,
) -> None:
    found = {
        name: owner
        for name, owner in owner_conn.execute(
            text(
                "SELECT viewname, viewowner FROM pg_views"
                " WHERE schemaname = 'finance' AND viewname LIKE 'report%'"
            )
        )
    }
    assert set(found) == REPORT_VIEWS
    assert set(found.values()) == {"finance_owner"}


def test_the_reader_selects_from_exactly_the_report_views_and_nothing_else(
    owner_conn: Connection,
) -> None:
    every = relations(owner_conn)
    assert len(every) > len(REPORT_VIEWS), "the control: base tables exist"
    held = {name: granted(owner_conn, name) for name in every}
    assert {name for name, privileges in held.items() if privileges} == REPORT_VIEWS
    for name in REPORT_VIEWS:
        assert held[name] == {"SELECT"}, name


def test_every_report_view_is_readable_under_the_reader_dsn(
    reader_engine: Engine,
) -> None:
    with reader_engine.connect() as connection:
        for name in sorted(REPORT_VIEWS):
            connection.execute(text(f"SELECT * FROM finance.{name} LIMIT 1")).all()


def _expect_denied(engine: Engine, statement: str) -> None:
    with engine.connect() as connection:
        with pytest.raises(DBAPIError) as caught:
            connection.execute(text(statement))
        connection.rollback()
    assert isinstance(caught.value.orig, psycopg.errors.InsufficientPrivilege), (
        statement
    )


@pytest.mark.parametrize(
    "statement",
    [
        # report_document is a single-table view, so Postgres would write through it
        # for a role holding the privilege; the reader does not.
        "INSERT INTO finance.report_document (path) VALUES ('probe')",
        "UPDATE finance.report_document SET path = 'probe'",
        "DELETE FROM finance.report_document",
        "INSERT INTO finance.document (path) VALUES ('probe')",
        "UPDATE finance.coverage SET state = 'have'",
        "DELETE FROM finance.archive_skip",
        "TRUNCATE finance.document",
        "CREATE TABLE finance.reader_probe (id integer)",
        "CREATE VIEW finance.report_probe AS SELECT 1 AS one",
        "DROP VIEW finance.report_status",
        "ALTER VIEW finance.report_coverage RENAME TO report_probe",
        "CREATE OR REPLACE VIEW finance.report_document AS SELECT 1 AS one",
    ],
)
def test_writes_and_ddl_by_the_reader_fail(
    reader_engine: Engine, statement: str
) -> None:
    _expect_denied(reader_engine, statement)


def test_no_report_view_column_carries_an_identifier_description_or_merchant() -> None:
    columns = [
        (table.name, column.name)
        for table in metadata.tables.values()
        if table.name.startswith("report_")
        for column in table.columns
    ]
    assert {name for name, _ in columns} == REPORT_VIEWS
    for table, column in columns:
        assert not restricted_by_name(column), (table, column)
        assert column not in NEVER_SHOWN, (table, column)
        assert tier_of(table, column) is Tier.PUBLIC, (table, column)


def test_every_reader_spec_runs_green_under_the_reader_dsn(
    make_context: MakeContext, sink_settings: Callable[..., None]
) -> None:
    """Key decision 21 both ways: every reader spec dispatched under the reader DSN
    returns its output model, as a member where it lists member."""
    specs = catalogue.load_all()
    readers = sorted(
        name for name, spec in specs.items() if spec.connection == "reader"
    )
    assert {
        "finance.sink.state",
        "finance.status.get",
        "finance.coverage.get",
        "finance.document.list",
    } <= set(readers)
    sink_settings()
    for name in readers:
        spec = specs[name]
        role = (
            Role.MEMBER
            if Role.MEMBER in spec.roles
            else min(spec.roles, key=lambda each: each.value)
        )
        out = dispatch(name, READER_INPUTS.get(name, {}), role, ctx=make_context(role))
        assert isinstance(out, spec.output_model), name
