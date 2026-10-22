"""AC-54a: every name that exists in the build has its shape.

Enumerated from the registries and the schema, never from a list here:

- record ids: every ``id`` or ``*_id`` column, in the ``MetaData`` and in the migrated
  database, is a uuid, and the two id makers return UUIDv7;
- references: ``module.type:id`` with a UUIDv7 id for a finance record;
- tool names: ``finance_<verb>``, from the operation registry;
- event types: ``finance.<type>.<past-verb>``, from each module's ``EVENT_MODELS``;
- job names: ``finance.<name>``, from each module's ``JOBS``;
- settings keys: ``finance.<section>.<key>``, from ``settings.SPECS``, a sink's
  ``finance.sinks.<sink>.enabled`` the only four-part key. The nine sink keys and the
  holdings-watch keys are among them.

The holdings-watch event ``finance.holdings.moved`` and job ``finance.holdings_watch``
join ``REQUIRED`` in the prompt that declares them.
"""

from __future__ import annotations

import datetime as dt
import re
import uuid
from collections.abc import Iterable, Mapping

import pytest
from sqlalchemy import Column, Connection, Integer, MetaData, Table, Text, Uuid, text

from rheo_finance import catalogue, settings
from rheo_finance.storage.ids import derived_id, uuid7
from rheo_finance.storage.refs import format_ref
from rheo_finance.storage.schema import metadata

from .source_tree import event_registries, job_names

_UUID7 = r"[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}"
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


def id_column_problems(meta: MetaData) -> list[str]:
    return sorted(
        f"{table.name}.{column.name} is {column.type}, not a uuid"
        for table in meta.tables.values()
        for column in table.columns
        if _is_id(column.name) and not isinstance(column.type, Uuid)
    )


def _is_id(name: str) -> bool:
    return name == "id" or name.endswith("_id")


def id_columns_in_database(conn: Connection) -> list[str]:
    rows = conn.execute(
        text(
            "SELECT table_name, column_name, data_type"
            " FROM information_schema.columns WHERE table_schema = 'finance'"
        )
    )
    return sorted(
        f"{table}.{column} is {data_type}, not a uuid"
        for table, column, data_type in rows
        if _is_id(column) and data_type != "uuid"
    )


def test_every_record_id_column_is_a_uuid(owner_conn: Connection) -> None:
    catalogue.load_all()
    assert id_column_problems(metadata) == []
    assert id_columns_in_database(owner_conn) == []
    # The control: there are id columns to check.
    assert any(
        _is_id(column.name)
        for table in metadata.tables.values()
        for column in table.columns
    )


def test_a_planted_id_column_that_is_not_a_uuid_is_found(
    owner_conn: Connection,
) -> None:
    fixture = MetaData(schema="finance")
    Table(
        "planted",
        fixture,
        Column("id", Integer, primary_key=True),
        Column("party_id", Text),
        Column("label", Text),
    )
    assert id_column_problems(fixture) == [
        "planted.id is INTEGER, not a uuid",
        "planted.party_id is TEXT, not a uuid",
    ]
    owner_conn.execute(text("CREATE TABLE finance.planted_ids (id bigint, x text)"))
    assert id_columns_in_database(owner_conn) == [
        "planted_ids.id is bigint, not a uuid"
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
