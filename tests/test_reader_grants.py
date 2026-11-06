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
from sqlalchemy import (
    Connection,
    Engine,
    String,
    Text,
    cast,
    func,
    inspect,
    literal,
    literal_column,
    select,
    text,
)
from sqlalchemy.exc import DBAPIError
from sqlalchemy.sql.selectable import SelectBase, SelectStatementGrouping

import reader_projection as projection
from conftest import DatabaseUnderTest
from guards.test_tiers import restricted_by_name
from reader_projection import MONEY, VIEWS, output_problems
from rheo_finance import catalogue, composition, operations
from rheo_finance.cli.main import main
from rheo_finance.context import FinanceContext
from rheo_finance.contracts import Role
from rheo_finance.operations import dispatch
from rheo_finance.sinks.views import view_sql
from rheo_finance.storage.schema import metadata, txn
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
        "report_open_item",
        "report_owed",
        "report_owed_trace",
        "report_txn",
        "report_crypto_movement",
        "report_queue_counts",
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
    assert set(VIEWS) == REPORT_VIEWS
    assert set(found.values()) == {"finance_owner"}


def test_migrated_columns_types_and_expressions_match_the_safe_projections(
    owner_conn: Connection,
) -> None:
    assert output_problems() == []
    type_problems: list[tuple[str, str, str, str]] = []
    for name, build in VIEWS.items():
        mirror = metadata.tables[f"finance.{name}"]
        actual = inspect(owner_conn).get_columns(name, schema="finance")
        assert {column["name"] for column in actual} == set(mirror.c.keys()), name
        for column in actual:
            expected = mirror.c[column["name"]]
            actual_type = column["type"].compile(dialect=owner_conn.dialect)
            expected_type = expected.type.compile(dialect=owner_conn.dialect)
            # PostgreSQL keeps the status union's schema-version subject as
            # unbounded VARCHAR; both it and TEXT implement the text mirror.
            if isinstance(expected.type, String):
                assert isinstance(column["type"], String), (name, column["name"])
                assert column["type"].length == expected.type.length, (
                    name,
                    column["name"],
                )
            else:
                if actual_type != expected_type:
                    type_problems.append(
                        (name, column["name"], actual_type, expected_type)
                    )
            assert tier_of(name, column["name"]) is not None
            assert column["nullable"] == expected.nullable, (name, column["name"])
        # Let PostgreSQL normalize both bodies, including casts and implicit joins.
        # The expected view is session-local and cannot change any reader grant.
        owner_conn.execute(text("DROP VIEW IF EXISTS pg_temp.reader_expected"))
        owner_conn.execute(
            text("CREATE TEMP VIEW reader_expected AS " + view_sql(build()))
        )
        actual_body = owner_conn.scalar(
            text("SELECT pg_get_viewdef(:name, true)"), {"name": f"finance.{name}"}
        )
        expected_body = owner_conn.scalar(
            text("SELECT pg_get_viewdef('pg_temp.reader_expected'::regclass, true)")
        )
        assert actual_body == expected_body, name
    owner_conn.execute(text("DROP VIEW pg_temp.reader_expected"))
    assert type_problems == []


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
        assert connection.scalar(text("SELECT current_user")) == READER
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
        # This report deliberately exposes money, with its restricted tier intact.
        monetary = (
            table in {"report_open_item", "report_txn", "report_crypto_movement"}
            and column in {"amount", "cad_amount"}
        ) or (
            table in {"report_owed", "report_owed_trace"}
            and column
            in {
                "amount_due",
                "minimum_due",
                "threshold_due",
                "paid",
                "remaining",
                "threshold_remaining",
                "manual_mismatch",
                "payments_mismatch",
                "applied_amount",
                "payment_amount",
            }
        )
        assert not restricted_by_name(column) or monetary, (table, column)
        assert column not in NEVER_SHOWN, (table, column)
        assert tier_of(table, column) is (
            Tier.RESTRICTED if monetary else Tier.PUBLIC
        ), (table, column)


# FR-48 exposes these monetary facts deliberately. No other restricted dependency
# is accepted, even if renamed or wrapped in a function in a view expression.
def test_no_report_view_depends_on_restricted_identity_even_through_an_alias(
    owner_conn: Connection,
) -> None:
    dependencies = owner_conn.execute(
        text("""
        WITH RECURSIVE dependencies(root, relation) AS (
            SELECT c.oid, c.oid FROM pg_class c
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = 'finance' AND c.relname LIKE 'report\\_%' ESCAPE '\\'
                AND c.relkind = 'v'
            UNION
            SELECT prior.root, d.refobjid FROM dependencies prior
            JOIN pg_rewrite r ON r.ev_class = prior.relation
            JOIN pg_depend d ON d.classid = 'pg_rewrite'::regclass
                AND d.objid = r.oid AND d.refclassid = 'pg_class'::regclass
            JOIN pg_class source ON source.oid = d.refobjid
            WHERE source.relkind IN ('v', 'm') AND source.oid <> prior.relation
        )
        SELECT DISTINCT v.relname, source_namespace.nspname, source.relname, a.attname
        FROM dependencies prior
        JOIN pg_class v ON v.oid = prior.root
        JOIN pg_rewrite r ON r.ev_class = prior.relation
        JOIN pg_depend d ON d.classid = 'pg_rewrite'::regclass
            AND d.objid = r.oid AND d.refclassid = 'pg_class'::regclass
        JOIN pg_class source ON source.oid = d.refobjid
        JOIN pg_namespace source_namespace ON source_namespace.oid = source.relnamespace
        LEFT JOIN pg_attribute a ON a.attrelid = source.oid AND a.attnum = d.refobjsubid
        WHERE source.relkind NOT IN ('v', 'm')
    """)
    )
    found: set[str] = set()
    for view, schema, table, column in dependencies:
        found.add(view)
        assert schema == "finance", (view, schema, table, column)
        assert f"{schema}.{table}" in metadata.tables or table == (
            "alembic_version_finance"
        ), (view, schema, table, column)
        if column is None:
            continue  # The relation itself, as well as its referenced attributes.
        tier = tier_of(table, column)
        # The Alembic revision is the only non-ledger column in these views.
        assert tier is not None or (table, column) == (
            "alembic_version_finance",
            "version_num",
        ), (view, table, column)
        allowed = set(MONEY.get(view, {}).values())
        if view == "report_status":
            allowed.add(("obligation", "manual_mismatch"))
        if view in {"report_owed", "report_owed_trace"}:
            allowed.add(("payee", "patterns"))
        assert tier is not Tier.RESTRICTED or (table, column) in allowed, (
            view,
            table,
            column,
        )
    assert found == REPORT_VIEWS
    assert output_problems() == []
    status_sql = view_sql(VIEWS["report_status"]())
    assert status_sql.count("finance.obligation.manual_mismatch") == 1
    assert "finance.obligation.manual_mismatch IS NOT NULL" in status_sql


def test_every_reader_spec_runs_green_under_the_reader_dsn(
    make_context: MakeContext,
    sink_settings: Callable[..., None],
    test_database: DatabaseUnderTest,
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
        "finance.openitem.list",
        "finance.owed.list",
        "finance.owed.trace",
        "finance.txn.list",
    } <= set(readers)
    assert specs["finance.txn.list"].tool_name == "finance_lines"
    sink_settings()
    for name in readers:
        spec = specs[name]
        role = (
            Role.MEMBER
            if Role.MEMBER in spec.roles
            else min(spec.roles, key=lambda each: each.value)
        )
        ctx = operations.with_databases(
            make_context(role),
            owner_url=test_database.owner_url.set(port=1),
            reader_url=test_database.reader_url,
        )
        out = dispatch(name, READER_INPUTS.get(name, {}), role, ctx=ctx)
        assert isinstance(out, spec.output_model), name


@pytest.mark.parametrize(
    "argv",
    [
        ["status", "--json"],
        ["owed", "--json"],
        ["coverage", "--json"],
        ["lines"],
        ["report", "open-items", "--year", "2026"],
    ],
)
def test_monitor_commands_work_with_only_the_actual_reader_dsn(
    argv: list[str],
    make_context: MakeContext,
    test_database: DatabaseUnderTest,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    sink_settings: Callable[..., None],
) -> None:
    catalogue.load_all()
    sink_settings()
    ctx = operations.with_databases(
        make_context(Role.OWNER),
        owner_url=test_database.owner_url.set(port=1),
        reader_url=test_database.reader_url,
    )
    monkeypatch.setattr(composition, "build_context", lambda _role: ctx)
    assert main(argv) == 0
    captured = capsys.readouterr()
    assert captured.out and captured.err == ""


def test_expression_proof_rejects_raw_stars_casts_and_hidden_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    queries: tuple[SelectBase, ...] = (
        select(literal_column("*")).select_from(txn),
        select(literal_column("finance.txn.description_norm")).select_from(txn),
        select(cast(txn.c.amount, Text).label("display_label")).select_from(txn),
        select(select(txn.c.description_norm).scalar_subquery().label("display_label")),
        select(func.string_agg(txn.c.description_norm, ",").label("display_label")),
    )
    for query in queries:
        monkeypatch.setattr(
            projection,
            "VIEWS",
            {"report_fixture": lambda value=query: value},
        )
        assert output_problems(), str(query)
    monkeypatch.setattr(
        projection,
        "VIEWS",
        {"report_fixture": lambda: select(func.count()).select_from(txn)},
    )
    assert output_problems() == []


@pytest.mark.parametrize(
    "shape",
    [
        "union",
        "union_all",
        "grouped_nested",
        "subquery",
        "alias",
        "scalar",
    ],
)
def test_expression_proof_rejects_restricted_output_in_later_compound_arms(
    shape: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    safe = select(literal("safe").label("display_label"))
    hidden = select(txn.c.description_norm.label("display_label"))
    combined = safe.union_all(hidden)
    nested = safe.union_all(safe.union_all(hidden))
    assert isinstance(nested.selects[1], SelectStatementGrouping)
    wrapped = nested.subquery("wrapped_lines")
    aliased = wrapped.alias("aliased_lines")
    queries: dict[str, SelectBase] = {
        "union": safe.union(hidden),
        "union_all": combined,
        "grouped_nested": nested,
        "subquery": select(wrapped.c.display_label),
        "alias": select(aliased.c.display_label),
        "scalar": select(nested.scalar_subquery().label("display_label")),
    }
    monkeypatch.setattr(projection, "VIEWS", {"report_fixture": lambda: queries[shape]})
    assert (
        "report_fixture.display_label: forbidden restricted source" in output_problems()
    )


def test_compound_arms_preserve_exact_money_sources_and_first_arm_output_names(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = select(
        txn.c.amount, txn.c.cad_amount, literal("safe").label("display_label")
    )
    later = select(
        txn.c.amount.label("renamed_amount"),
        txn.c.cad_amount.label("renamed_cad"),
        literal("also safe").label("renamed_label"),
    )
    monkeypatch.setattr(
        projection, "VIEWS", {"report_txn": lambda: first.union_all(later)}
    )
    assert output_problems() == []
