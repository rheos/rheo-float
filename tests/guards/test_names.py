"""AC-54a: every name that exists in the build has its shape.

Enumerated from the registries and the schema, never from a list here:

- record ids: every ``id`` primary key and every column with a foreign key inside
  schema ``finance``, in the ``MetaData`` and in the migrated database, is a uuid
  (an ``external_id`` or ``parser_id`` with no key is not a record id), and the two id
  makers return UUIDv7;
- references: ``module.type:id`` with a UUIDv7 id for a finance record;
- tool names: ``finance_<verb>``, from the operation registry;
- event types: ``finance.<type>.<past-verb>``, from each module's ``EVENT_MODELS``;
- job names: ``finance.<name>``, from each module's ``JOBS``. Once a
  ``rheo_finance.jobs`` package exists, a module in it must declare ``JOBS`` and the
  jobs found must not be empty, so a registry the scan cannot see fails closed;
- settings keys: ``finance.<section>.<key>``, from ``settings.SPECS``, a sink's
  ``finance.sinks.<sink>.enabled`` the only four-part key. The nine sink keys and the
  holdings-watch keys are among them.

The holdings-watch event ``finance.holdings.moved`` and job ``finance.holdings_watch``
join ``REQUIRED`` in the prompt that declares them.
"""

from __future__ import annotations

import ast
import datetime as dt
import re
import uuid
from collections.abc import Iterable, Mapping
from pathlib import Path

import pytest
from sqlalchemy import (
    Column,
    Connection,
    ForeignKey,
    Integer,
    MetaData,
    Table,
    Text,
    Uuid,
    text,
)

from rheo_finance import catalogue, settings
from rheo_finance.storage.ids import derived_id, uuid7
from rheo_finance.storage.refs import format_ref
from rheo_finance.storage.schema import metadata

from .source_tree import (
    PACKAGE,
    copy_package,
    event_registries,
    job_names,
    modules,
    names_module,
    plant,
)

_UUID7 = r"[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}"
SCHEMA = "finance"
_NAME = r"[a-z][a-z0-9_]*"
_IRREGULAR_PAST = (
    "went|sent|began|paid|rose|fell|held|lost|came|left|ran|won|got|made|took|grew"
    "|brought|bought|spent|split|set|cut|put|read|shut"
)
SHAPES: dict[str, re.Pattern[str]] = {
    "id": re.compile(_UUID7),
    # A finance record's id is a UUIDv7; another module's id is its own opaque text.
    "ref": re.compile(
        rf"finance\.{_NAME}:{_UUID7}|(?!finance\.){_NAME}\.{_NAME}:[^\s:]+"
    ),
    "tool": re.compile(r"finance_[a-z][a-z0-9_]*"),
    "event": re.compile(
        rf"finance\.{_NAME}\.(?:[a-z]+ed|{_IRREGULAR_PAST})(?:_[a-z0-9]+)*"
    ),
    "job": re.compile(rf"finance\.{_NAME}"),
    "setting": re.compile(
        rf"finance\.(?!sinks\.){_NAME}\.{_NAME}|finance\.sinks\.{_NAME}\.enabled"
    ),
}
REQUIRED: dict[str, frozenset[str]] = {
    "event": frozenset({"finance.document.rejected"}),
    "setting": frozenset(
        {f"finance.sinks.{sink}.enabled" for sink in settings.SINKS}
        | {
            "finance.holdings.watch_hours",
            "finance.holdings.alert_pct",
            "finance.holdings.top_movers",
            "finance.coinstats.credit_budget",
        }
    ),
}


def name_problems(
    names: Mapping[str, Iterable[str]],
    required: Mapping[str, frozenset[str]] = REQUIRED,
) -> list[str]:
    problems: list[str] = []
    for kind, values in names.items():
        listed = list(values)
        shape = SHAPES[kind]
        problems += [
            f"{kind} {value!r} is not {shape.pattern}"
            for value in listed
            if not shape.fullmatch(value)
        ]
        problems += [f"{kind} {value!r} is listed twice" for value in _twice(listed)]
        problems += [
            f"{kind} {value!r} is missing"
            for value in sorted(required.get(kind, frozenset()) - set(listed))
        ]
    return problems


def _twice(values: list[str]) -> list[str]:
    return sorted({value for value in values if values.count(value) > 1})


def names_in_the_build() -> dict[str, list[str]]:
    specs = catalogue.load_all()
    return {
        "id": [str(uuid7()), str(derived_id(dt.date(2026, 1, 15), ("probe", 1)))],
        "ref": [
            format_ref("finance", "decision", uuid7()),
            format_ref("relationships", "party", uuid.uuid4()),
            format_ref("current", "item", uuid.uuid4()),
        ],
        "tool": [spec.tool_name for spec in specs.values() if spec.tool_name],
        "event": sorted(event_registries()),
        "job": sorted(job_names()),
        "setting": [spec.key for spec in settings.SPECS],
    }


def test_every_name_in_the_build_has_its_shape() -> None:
    names = names_in_the_build()
    # The control: the registries the build has today are read.
    assert names["tool"] and len(names["setting"]) > len(settings.SINKS)
    assert name_problems(names) == []


def test_the_nine_sinks_each_have_a_key() -> None:
    assert len(settings.SINKS) == 9 == len(set(settings.SINKS))


def _is_record_id(column: Column[object]) -> bool:
    """A record id: a table's ``id`` primary key, or a column whose foreign key points
    at a table in schema ``finance``. A ``*_id`` column with no key (an external id, a
    parser id, a mail message id) is not one, and neither is a natural key such as
    ``sink_spec.sink`` or the ``seq`` part of a composite key."""
    if column.primary_key and column.name == "id":
        return True
    return any(
        key.target_fullname.split(".")[0] == SCHEMA for key in column.foreign_keys
    )


def id_column_problems(meta: MetaData) -> list[str]:
    return sorted(
        f"{table.name}.{column.name} is {type(column.type).__name__}, not a uuid"
        for table in meta.tables.values()
        for column in table.columns
        if _is_record_id(column) and not isinstance(column.type, Uuid)
    )


RECORD_ID_COLUMNS = text(
    "SELECT DISTINCT rel.relname, att.attname, format_type(att.atttypid, att.atttypmod)"
    " FROM pg_constraint c"
    " JOIN pg_class rel ON rel.oid = c.conrelid"
    " JOIN pg_namespace ns ON ns.oid = rel.relnamespace"
    " JOIN LATERAL unnest(c.conkey) AS k(attnum) ON true"
    " JOIN pg_attribute att ON att.attrelid = rel.oid AND att.attnum = k.attnum"
    " LEFT JOIN pg_class target ON target.oid = c.confrelid"
    " LEFT JOIN pg_namespace target_ns ON target_ns.oid = target.relnamespace"
    " WHERE ns.nspname = :schema AND ("
    "  (c.contype = 'p' AND att.attname = 'id')"
    "  OR (c.contype = 'f' AND target_ns.nspname = :schema))"
)


def record_ids_in_database(conn: Connection) -> list[tuple[str, str, str]]:
    return sorted(
        (str(table), str(column), str(kind))
        for table, column, kind in conn.execute(RECORD_ID_COLUMNS, {"schema": SCHEMA})
    )


def id_columns_in_database(conn: Connection) -> list[str]:
    return [
        f"{table}.{column} is {kind}, not a uuid"
        for table, column, kind in record_ids_in_database(conn)
        if kind != "uuid"
    ]


def test_every_record_id_column_is_a_uuid(owner_conn: Connection) -> None:
    catalogue.load_all()
    assert id_column_problems(metadata) == []
    assert id_columns_in_database(owner_conn) == []
    # The control: there are record ids to check, in both places.
    assert any(
        _is_record_id(column)
        for table in metadata.tables.values()
        for column in table.columns
    )
    assert ("decision", "id", "uuid") in record_ids_in_database(owner_conn)


def test_a_planted_record_id_that_is_not_a_uuid_is_found() -> None:
    fixture = MetaData(schema=SCHEMA)
    Table(
        "planted",
        fixture,
        Column("id", Integer, primary_key=True),
        Column("about", Text, ForeignKey("finance.decision.id")),
        Column("label", Text),
    )
    assert id_column_problems(fixture) == [
        "planted.about is Text, not a uuid",
        "planted.id is Integer, not a uuid",
    ]


def test_an_id_named_column_that_is_not_a_record_id_passes() -> None:
    # The other direction: external, parser and message ids are plain text, and a
    # natural or composite key part is not a record id.
    fixture = MetaData(schema=SCHEMA)
    Table(
        "planted",
        fixture,
        Column("id", Uuid, primary_key=True),
        Column("external_id", Text),
        Column("parser_id", Text),
        Column("message_id", Text),
    )
    Table("planted_spec", fixture, Column("sink", Text, primary_key=True))
    Table(
        "planted_item",
        fixture,
        Column("planted_id", Uuid, ForeignKey("finance.planted.id"), primary_key=True),
        Column("seq", Integer, primary_key=True),
    )
    assert id_column_problems(fixture) == []


def test_record_ids_in_the_database_are_found_both_ways(owner_conn: Connection) -> None:
    # Rolled back with the test's transaction.
    owner_conn.execute(
        text(
            "CREATE TABLE finance.planted_parent (id bigint PRIMARY KEY);"
            "CREATE TABLE finance.planted_child (id uuid PRIMARY KEY,"
            " parent_id bigint REFERENCES finance.planted_parent (id),"
            " external_id text, message_id text);"
            "CREATE TABLE finance.planted_spec (sink text PRIMARY KEY, parser_id text)"
        )
    )
    assert id_columns_in_database(owner_conn) == [
        "planted_child.parent_id is bigint, not a uuid",
        "planted_parent.id is bigint, not a uuid",
    ]


@pytest.mark.parametrize(
    ("kind", "value"),
    [
        ("id", str(uuid.uuid4())),
        ("id", "0190a1b2-c3d4-7e5f-0a1b-2c3d4e5f6a7b"),  # variant bits wrong
        ("ref", "finance:0190a1b2"),
        ("ref", "Finance.txn:x"),
        ("ref", "finance.txn:not-a-uuid"),
        ("ref", f"finance.txn:{uuid.uuid4()}"),
        ("tool", "status"),
        ("tool", "finance.status"),
        ("event", "finance.payment.detect"),
        ("event", "finance.payment"),
        ("event", "payment.detected"),
        ("job", "backup"),
        ("job", "finance.jobs.backup"),
        ("setting", "finance.locale"),
        ("setting", "finance.a.b.c"),
        ("setting", "finance.sinks.mot.disabled"),
        ("setting", "finance.sinks.enabled"),
        ("setting", "finance.Locale.timezone"),
    ],
)
def test_a_planted_nonconforming_name_is_found(kind: str, value: str) -> None:
    assert name_problems({kind: [value]}, {}) != []


def test_the_spec_names_conform() -> None:
    # The control: the names SPEC lists for later prompts pass the same shapes.
    names = {
        "event": [
            "finance.document.rejected", "finance.fee.detected",
            "finance.overdraft.started", "finance.payment.reversed",
            "finance.statement.went_overdue", "finance.obligation.superseded",
            "finance.withdrawal.flagged", "finance.holdings.moved",
        ],
        "job": ["finance.derive", "finance.holdings_watch", "finance.mail_intake"],
        "tool": ["finance_sinks", "finance_owed"],
    }  # fmt: skip
    assert name_problems(names, {}) == []


def test_a_missing_or_doubled_required_name_is_found() -> None:
    keys = [
        spec.key for spec in settings.SPECS if spec.key != "finance.sinks.mot.enabled"
    ]
    assert name_problems({"setting": keys}) == [
        "setting 'finance.sinks.mot.enabled' is missing"
    ]
    assert name_problems({"job": ["finance.derive", "finance.derive"]}, {}) == [
        "job 'finance.derive' is listed twice"
    ]


JOBS_PACKAGE = "rheo_finance.jobs"


def _declares(tree: ast.Module, name: str) -> bool:
    for node in ast.walk(tree):
        targets = (
            node.targets
            if isinstance(node, ast.Assign)
            else [node.target]
            if isinstance(node, ast.AnnAssign)
            else []
        )
        if any(
            isinstance(target, ast.Name) and target.id == name for target in targets
        ):
            return True
    return False


def job_registry_problems(root: Path, names: list[str]) -> list[str]:
    """Fail closed: a jobs package with no ``JOBS`` the scan finds is a failure."""
    if not (root / "jobs" / "__init__.py").exists():
        return []
    problems: list[str] = []
    jobs_modules = [m for m in modules(root) if names_module(m.name, JOBS_PACKAGE)]
    if not any(_declares(module.tree, "JOBS") for module in jobs_modules):
        problems.append("rheo_finance.jobs exists but no module in it declares JOBS")
    if not names:
        problems.append("rheo_finance.jobs exists but no job is found")
    return problems


def test_a_jobs_package_declares_its_jobs() -> None:
    assert job_registry_problems(PACKAGE, job_names()) == []


def test_a_jobs_package_with_no_jobs_found_fails(tmp_path: Path) -> None:
    root = copy_package(tmp_path)
    assert job_registry_problems(root, []) == []  # no jobs package yet
    plant(root, "jobs/tick.py", "def tick() -> None: ...\n")
    assert job_registry_problems(root, []) == [
        "rheo_finance.jobs exists but no module in it declares JOBS",
        "rheo_finance.jobs exists but no job is found",
    ]
    plant(root, "jobs/__init__.py", "JOBS: tuple[str, ...] = ()\n")
    assert job_registry_problems(root, []) == [
        "rheo_finance.jobs exists but no job is found"
    ]
    assert job_registry_problems(root, ["finance.derive"]) == []
