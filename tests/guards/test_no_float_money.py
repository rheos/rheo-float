"""AC-14: money is never a float.

No column in schema ``finance`` (tables and views, as the migrated database has them,
and as the shared ``MetaData`` declares them) is ``real``, ``double precision`` or a
SQLAlchemy float type. No module in ``src`` calls ``float(``, and none imports a float
column type from SQLAlchemy.
"""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import (
    Column,
    Connection,
    Float,
    MetaData,
    Numeric,
    Table,
    Text,
    text,
)
from sqlalchemy.types import TypeEngine

from rheo_finance import catalogue
from rheo_finance.storage.schema import metadata

from .source_tree import PACKAGE, calls, copy_package, imports, modules, plant

FLOAT_TYPES = ("real", "double precision")
# SQLAlchemy's float column types, as importable names.
FLOAT_NAMES = {"Float", "REAL", "DOUBLE", "DOUBLE_PRECISION", "FLOAT", "Double"}


def _is_float_type(column_type: TypeEngine[object]) -> bool:
    # Float subclasses Numeric; a Numeric that is not a Float is exact.
    return isinstance(column_type, Float) or (
        isinstance(column_type, Numeric) and column_type.asdecimal is False
    )


def float_columns_in(meta: MetaData) -> list[str]:
    return sorted(
        f"{table.name}.{column.name}"
        for table in meta.tables.values()
        for column in table.columns
        if _is_float_type(column.type)
    )


def float_columns_in_database(conn: Connection) -> list[str]:
    rows = conn.execute(
        text(
            "SELECT table_name, column_name FROM information_schema.columns"
            " WHERE table_schema = 'finance' AND data_type = ANY(:types)"
        ),
        {"types": list(FLOAT_TYPES)},
    )
    return sorted(f"{table}.{column}" for table, column in rows)


def float_uses(root: Path = PACKAGE) -> list[str]:
    found: list[str] = []
    for module in modules(root):
        for name, call in calls(module):
            if name in {"float", "builtins.float"}:
                found.append(f"{module.name}:{call.lineno} float(")
        for each in imports(module):
            head, _, last = each.name.rpartition(".")
            if head.split(".")[0] == "sqlalchemy" and last in FLOAT_NAMES:
                found.append(f"{module.name}:{each.line} imports {each.name}")
    return found


# --- the real tree --------------------------------------------------------------------


def test_no_float_column_in_the_metadata() -> None:
    catalogue.load_all()
    assert float_columns_in(metadata) == []


def test_no_float_column_in_the_migrated_database(owner_conn: Connection) -> None:
    assert float_columns_in_database(owner_conn) == []


def test_no_module_converts_to_float() -> None:
    assert float_uses() == []


# --- each check fails on a planted violation ------------------------------------------


def test_a_float_column_in_a_fixture_metadata_is_found() -> None:
    fixture = MetaData(schema="finance")
    Table(
        "planted",
        fixture,
        Column("label", Text),
        Column("amount", Float),
        Column("rate", Numeric(asdecimal=False)),
        Column("cad", Numeric(20, 2)),
    )
    assert float_columns_in(fixture) == ["planted.amount", "planted.rate"]


def test_a_real_column_in_the_database_is_found(owner_conn: Connection) -> None:
    # Rolled back with the test's transaction.
    owner_conn.execute(
        text(
            "CREATE TABLE finance.planted_money (a real, b double precision, c numeric)"
        )
    )
    assert float_columns_in_database(owner_conn) == [
        "planted_money.a",
        "planted_money.b",
    ]


def test_a_planted_float_conversion_is_found(tmp_path: Path) -> None:
    root = copy_package(tmp_path)
    plant(root, "planted.py", "def total(row):\n    return float(row.amount)\n")
    plant(
        root,
        "storage/planted.py",
        "from sqlalchemy import Float\nfrom sqlalchemy.types import DOUBLE_PRECISION\n",
    )
    assert float_uses(root) == [
        "rheo_finance.planted:2 float(",
        "rheo_finance.storage.planted:1 imports sqlalchemy.Float",
        "rheo_finance.storage.planted:2 imports sqlalchemy.types.DOUBLE_PRECISION",
    ]
