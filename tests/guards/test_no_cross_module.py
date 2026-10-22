"""AC-57a: no tie to another module but plain-text references.

- No foreign key crosses the boundary of schema ``finance``, in either direction, in
  the migrated database; and none in the ``MetaData`` names a table outside it.
- No module imports another module's package (``rheo_core``, ``rheo_contracts``,
  ``rheo_stream``, ``rheostream`` or any other ``rheo*`` package but ``rheo_finance``),
  and ``pyproject.toml`` and ``uv.lock`` name none.
- An external reference is a plain text column: every ``*_ref`` column is text with no
  foreign key.
"""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import (
    Column,
    Connection,
    ForeignKey,
    MetaData,
    Table,
    Text,
    Uuid,
    create_engine,
    text,
)
from sqlalchemy.pool import NullPool

from conftest import DatabaseUnderTest
from rheo_finance import catalogue, db
from rheo_finance.storage.schema import metadata

from .source_tree import (
    PACKAGE,
    PYPROJECT,
    SCRIPTS,
    UV_LOCK,
    copy_package,
    declared_packages,
    imports,
    modules,
    plant,
)

SCHEMA = "finance"
OWN_PACKAGE = "rheo_finance"
OWN_DISTRIBUTION = "rheo-finance"


def crossing_foreign_keys(conn: Connection) -> list[str]:
    rows = conn.execute(
        text(
            "SELECT c.conname, src.nspname, dst.nspname"
            " FROM pg_constraint c"
            " JOIN pg_class sc ON sc.oid = c.conrelid"
            " JOIN pg_namespace src ON src.oid = sc.relnamespace"
            " JOIN pg_class dc ON dc.oid = c.confrelid"
            " JOIN pg_namespace dst ON dst.oid = dc.relnamespace"
            " WHERE c.contype = 'f'"
            " AND (src.nspname = :schema) <> (dst.nspname = :schema)"
        ),
        {"schema": SCHEMA},
    )
    return sorted(f"{name}: {source} -> {target}" for name, source, target in rows)


def metadata_foreign_key_problems(meta: MetaData) -> list[str]:
    return sorted(
        f"{table.name}.{key.parent.name} -> {key.target_fullname}"
        for table in meta.tables.values()
        for key in table.foreign_keys
        if key.target_fullname.split(".")[0] != SCHEMA
    )


def ref_column_problems(meta: MetaData) -> list[str]:
    problems: list[str] = []
    for table in meta.tables.values():
        for column in table.columns:
            if not column.name.endswith("_ref"):
                continue
            if not isinstance(column.type, Text):
                kind = type(column.type).__name__
                problems.append(f"{table.name}.{column.name} is {kind}, not text")
            if column.foreign_keys:
                problems.append(f"{table.name}.{column.name} has a foreign key")
    return sorted(problems)


def is_other_module(name: str) -> bool:
    top = name.split(".")[0].lower()
    return top.startswith("rheo") and top != OWN_PACKAGE


def foreign_module_imports(*roots: Path) -> list[str]:
    return sorted(
        {
            f"{module.name}: {each.name.split('.')[0]}"
            for root in roots
            for module in modules(root)
            for each in imports(module)
            if is_other_module(each.name)
        }
    )


def foreign_distributions(
    pyproject: Path = PYPROJECT, lock: Path = UV_LOCK
) -> list[str]:
    return sorted(
        name
        for name in declared_packages(pyproject, lock)
        if name.startswith("rheo") and name != OWN_DISTRIBUTION
    )


# --- the real tree --------------------------------------------------------------------


def test_no_foreign_key_crosses_the_schema(owner_conn: Connection) -> None:
    assert crossing_foreign_keys(owner_conn) == []


def test_no_metadata_foreign_key_leaves_the_schema() -> None:
    catalogue.load_all()
    assert metadata_foreign_key_problems(metadata) == []
    # The control: the decision tables do have keys inside the schema.
    assert any(table.foreign_keys for table in metadata.tables.values())


def test_external_references_are_plain_text() -> None:
    catalogue.load_all()
    assert ref_column_problems(metadata) == []


def test_no_module_imports_another_module() -> None:
    assert foreign_module_imports(PACKAGE, SCRIPTS) == []


def test_no_other_module_is_a_dependency() -> None:
    assert foreign_distributions() == []
    # The control: the package itself is in the lock and is not counted.
    assert OWN_DISTRIBUTION in declared_packages()


# --- each check fails on a planted violation ------------------------------------------


def test_a_planted_crossing_foreign_key_is_found(
    scratch_database: DatabaseUnderTest,
) -> None:
    # The owner role cannot create a schema, and a temporary table cannot reference a
    # permanent one, so the superuser plants both directions in the throwaway database,
    # which is dropped after the module.
    db.psql(
        "CREATE SCHEMA planted;"
        " CREATE TABLE planted.link"
        " (decision_id uuid REFERENCES finance.decision (id));"
        " CREATE TABLE planted.parent (id integer PRIMARY KEY);"
        " CREATE TABLE finance.planted_child"
        " (p integer REFERENCES planted.parent (id));",
        database=str(scratch_database.owner_url.database),
    )
    engine = create_engine(scratch_database.owner_url, poolclass=NullPool)
    try:
        with engine.connect() as conn:
            found = crossing_foreign_keys(conn)
    finally:
        engine.dispose()
    assert sorted(each.split(": ")[1] for each in found) == [
        "finance -> planted",
        "planted -> finance",
    ]


def test_a_planted_metadata_key_and_ref_column_are_found() -> None:
    fixture = MetaData(schema=SCHEMA)
    Table(
        "party", fixture, Column("id", Uuid, primary_key=True), schema="relationships"
    )
    Table(
        "planted",
        fixture,
        Column("id", Uuid, primary_key=True),
        Column("party_ref", Uuid, ForeignKey("relationships.party.id")),
        Column("item_ref", Text),
    )
    assert metadata_foreign_key_problems(fixture) == [
        "planted.party_ref -> relationships.party.id"
    ]
    assert ref_column_problems(fixture) == [
        "planted.party_ref has a foreign key",
        "planted.party_ref is Uuid, not text",
    ]


def test_a_planted_import_of_another_module_is_found(tmp_path: Path) -> None:
    root = copy_package(tmp_path)
    plant(
        root,
        "planted.py",
        "import rheo_core\nfrom rheo_contracts.party import Party\n"
        "def later():\n    import rheostream.modules\n",
    )
    assert foreign_module_imports(root) == [
        "rheo_finance.planted: rheo_contracts",
        "rheo_finance.planted: rheo_core",
        "rheo_finance.planted: rheostream",
    ]


def test_a_planted_dependency_on_another_module_is_found(tmp_path: Path) -> None:
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        '[project]\nname = "rheo-finance"\ndependencies = ["rheo_core>=1"]\n',
        encoding="utf-8",
    )
    lock = tmp_path / "uv.lock"
    lock.write_text(
        '[[package]]\nname = "rheo-stream"\nversion = "1"\n', encoding="utf-8"
    )
    assert foreign_distributions(pyproject, lock) == ["rheo-core", "rheo-stream"]
