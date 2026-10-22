"""``finance db``: init idempotence, role privileges, version table, compose pins.

These run against the local container (``finance db up``). The init tests run as the
container superuser through ``db.psql``; the privilege tests use database
``finance_test`` through the conftest fixtures.
"""

from __future__ import annotations

import json
import re
import secrets
import stat
import subprocess
from collections.abc import Iterator, Sequence
from pathlib import Path

import psycopg
import pytest
from alembic import command
from sqlalchemy import Connection, Engine, text
from sqlalchemy.exc import DBAPIError

from conftest import DatabaseUnderTest
from rheo_finance import db
from rheo_finance.cli.main import main

# Unique per session: two suites on the shared container must not drop each other's.
PROBE_DATABASE = f"finance_init_probe_{secrets.token_hex(4)}"


class LiveDatabaseTouched(AssertionError):
    pass


@pytest.fixture(autouse=True)
def never_the_live_database(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every superuser statement a test sends must leave the live ledger alone."""
    real_psql = db.psql

    def guarded(
        sql: str, *, database: str = "postgres", hidden: Sequence[str] = ()
    ) -> str:
        if database == db.LIVE_DATABASE or f'"{db.LIVE_DATABASE}"' in sql:
            raise LiveDatabaseTouched(f"a test aimed SQL at {db.LIVE_DATABASE}")
        return real_psql(sql, database=database, hidden=hidden)

    monkeypatch.setattr(db, "psql", guarded)


def _literal_list(names: Sequence[str]) -> str:
    return ", ".join("'" + name + "'" for name in names)


def _catalog_snapshot(databases: Sequence[str]) -> list[str]:
    """Everything ``db init`` sets: role attributes and password hashes (digested),
    database owners and ACLs, schema owners and ACLs, default privileges, and
    per-role settings."""
    rows = db.psql(
        "SELECT rolname, rolsuper, rolinherit, rolcreaterole, rolcreatedb, rolcanlogin,"
        " rolreplication, rolbypassrls, rolconnlimit, md5(coalesce(rolpassword, ''))"
        " FROM pg_authid WHERE rolname IN ('finance_owner', 'finance_reader')"
        " ORDER BY rolname;"
    ).splitlines()
    rows += db.psql(
        "SELECT datname, pg_get_userbyid(datdba), coalesce(datacl::text, '')"
        f" FROM pg_database WHERE datname IN ({_literal_list(databases)})"
        " ORDER BY datname;"
    ).splitlines()
    rows += db.psql(
        "SELECT setdatabase, setrole, setconfig::text FROM pg_db_role_setting"
        " ORDER BY 1, 2;"
    ).splitlines()
    for database in databases:
        rows += [
            f"{database}|{row}"
            for row in db.psql(
                "SELECT nspname, pg_get_userbyid(nspowner), coalesce(nspacl::text, '')"
                " FROM pg_namespace WHERE nspname NOT LIKE 'pg_%'"
                " AND nspname <> 'information_schema' ORDER BY nspname;",
                database=database,
            ).splitlines()
        ]
        rows += [
            f"{database}|default|{row}"
            for row in db.psql(
                "SELECT pg_get_userbyid(defaclrole), defaclnamespace, defaclobjtype,"
                " coalesce(defaclacl::text, '') FROM pg_default_acl ORDER BY 1, 2, 3;",
                database=database,
            ).splitlines()
        ]
    return rows


def _drop_probe() -> None:
    db.psql(f'DROP DATABASE IF EXISTS "{PROBE_DATABASE}" WITH (FORCE);')


@pytest.fixture
def probe_database() -> Iterator[str]:
    _drop_probe()
    yield PROBE_DATABASE
    _drop_probe()


def _has(sql: str, database: str) -> bool:
    return db.psql(sql, database=database).strip() == "t"


# --- finance db init ---------------------------------------------------------------


def test_the_guard_stops_init_before_it_reaches_the_live_database() -> None:
    with pytest.raises(LiveDatabaseTouched):
        db.init()  # the default names the live ledger


def test_db_init_creates_a_database_then_a_second_run_changes_nothing(
    probe_database: str,
) -> None:
    first = db.init(databases=(probe_database,))
    assert f"created database {probe_database}" in first
    assert f"created schema finance in {probe_database}" in first
    assert _has(
        "SELECT pg_get_userbyid(nspowner) = 'finance_owner' FROM pg_namespace"
        " WHERE nspname = 'finance';",
        probe_database,
    )
    assert _has(
        "SELECT has_schema_privilege('finance_reader', 'finance', 'USAGE')"
        " AND NOT has_schema_privilege('finance_reader', 'finance', 'CREATE')"
        " AND NOT has_schema_privilege('finance_reader', 'public', 'CREATE')"
        f" AND NOT has_database_privilege('finance_reader', '{probe_database}', 'TEMP')"
        f" AND NOT has_database_privilege('finance_reader', '{probe_database}',"
        " 'CREATE');",
        probe_database,
    )

    before = _catalog_snapshot((probe_database,))
    assert db.init(databases=(probe_database,)) == []
    assert _catalog_snapshot((probe_database,)) == before


def test_finance_db_init_twice_is_a_no_op_and_prints_no_password(
    capsys: pytest.CaptureFixture[str], probe_database: str
) -> None:
    scoped = (db.TEST_DATABASE, probe_database)
    assert db.LIVE_DATABASE not in scoped
    argv = ["db", "init", "--database", scoped[0], "--database", scoped[1]]

    assert main(argv) == 0
    first = capsys.readouterr().out
    assert f"- created database {probe_database}" in first.splitlines()
    before = _catalog_snapshot(scoped)

    assert main(argv) == 0
    out = capsys.readouterr().out
    assert _catalog_snapshot(scoped) == before
    assert out.splitlines() == ["finance db init: already set up, nothing changed."]

    values = db.read_env_file()
    passwords = [
        values[db.PASSWORD_KEY],
        db.to_sqlalchemy_url(values[db.OWNER_DSN_KEY]).password or "",
        db.to_sqlalchemy_url(values[db.READER_DSN_KEY]).password or "",
    ]
    leaked = any(value and value in first + out for value in passwords)
    assert not leaked, "db init printed a password"


def test_db_init_refuses_a_name_it_would_have_to_quote() -> None:
    with pytest.raises(db.DbError):
        db.init(databases=('finance_test"; DROP TABLE x; --',))


# --- role privileges -----------------------------------------------------------------


def _expect_denied(engine: Engine, statement: str) -> None:
    with engine.connect() as connection:
        with pytest.raises(DBAPIError) as caught:
            connection.execute(text(statement))
        connection.rollback()
    assert isinstance(caught.value.orig, psycopg.errors.InsufficientPrivilege), (
        statement
    )


def test_reader_can_connect_with_usage_only(reader_engine: Engine) -> None:
    with reader_engine.connect() as connection:
        row = connection.execute(
            text(
                "SELECT current_user, current_database(),"
                " has_schema_privilege('finance', 'USAGE'),"
                " has_schema_privilege('finance', 'CREATE')"
            )
        ).one()
    assert tuple(row) == ("finance_reader", db.TEST_DATABASE, True, False)


@pytest.mark.parametrize(
    "statement",
    [
        "CREATE TABLE finance.reader_probe (id integer)",
        "CREATE VIEW finance.reader_probe AS SELECT 1 AS one",
        "CREATE TABLE public.reader_probe (id integer)",
        "CREATE TEMPORARY TABLE reader_probe (id integer)",
        "CREATE SCHEMA reader_probe",
    ],
)
def test_reader_cannot_run_ddl(reader_engine: Engine, statement: str) -> None:
    _expect_denied(reader_engine, statement)


def test_reader_cannot_read_or_write_any_finance_table(
    owner_conn: Connection, reader_engine: Engine
) -> None:
    tables = owner_conn.execute(
        text(
            "SELECT c.relname, a.attname FROM pg_class c"
            " JOIN pg_namespace n ON n.oid = c.relnamespace"
            " JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum = 1"
            " WHERE n.nspname = 'finance' AND c.relkind IN ('r', 'p')"
            " ORDER BY c.relname"
        )
    ).all()
    assert tables, "the migrated schema has no table to probe"
    for table, column in tables:
        name = f'finance."{table}"'
        for statement in (
            f"INSERT INTO {name} DEFAULT VALUES",
            f'UPDATE {name} SET "{column}" = "{column}"',
            f"DELETE FROM {name}",
            f"TRUNCATE {name}",
            f"SELECT * FROM {name}",
        ):
            _expect_denied(reader_engine, statement)


def test_reader_cannot_call_a_function_the_owner_creates(
    owner_engine: Engine, reader_engine: Engine
) -> None:
    db.init(databases=(db.TEST_DATABASE,))  # applies the default privileges
    name = f"finance.exec_probe_{secrets.token_hex(4)}"
    with owner_engine.begin() as connection:
        connection.execute(
            text(f"CREATE FUNCTION {name}() RETURNS integer LANGUAGE sql AS 'SELECT 1'")
        )
    try:
        with owner_engine.connect() as connection:
            assert connection.execute(text(f"SELECT {name}()")).scalar_one() == 1
        _expect_denied(reader_engine, f"SELECT {name}()")
    finally:
        with owner_engine.begin() as connection:
            connection.execute(text(f"DROP FUNCTION IF EXISTS {name}()"))


def test_owner_writes_inside_its_rolled_back_transaction(
    owner_conn: Connection, owner_engine: Engine
) -> None:
    owner_conn.execute(text("CREATE TABLE finance.rollback_probe (id integer)"))
    owner_conn.execute(text("INSERT INTO finance.rollback_probe VALUES (1)"))
    count = owner_conn.execute(text("SELECT count(*) FROM finance.rollback_probe"))
    assert count.scalar_one() == 1
    with owner_engine.connect() as other:
        seen = other.execute(text("SELECT to_regclass('finance.rollback_probe')"))
        assert seen.scalar_one() is None


def test_the_previous_test_left_nothing_behind(owner_engine: Engine) -> None:
    # Runs after the test above (file order): its table must be gone.
    with owner_engine.connect() as connection:
        seen = connection.execute(text("SELECT to_regclass('finance.rollback_probe')"))
        assert seen.scalar_one() is None


def test_owner_cannot_create_outside_schema_finance(owner_engine: Engine) -> None:
    _expect_denied(owner_engine, "CREATE TABLE public.owner_probe (id integer)")
    _expect_denied(owner_engine, "CREATE SCHEMA owner_probe")


# --- the migration chain -----------------------------------------------------------


def test_version_table_is_finance_alembic_version_finance(
    owner_conn: Connection, test_database: DatabaseUnderTest
) -> None:
    schemas = owner_conn.execute(
        text(
            "SELECT schemaname FROM pg_tables WHERE tablename = :name"
            " ORDER BY schemaname"
        ),
        {"name": db.VERSION_TABLE},
    ).scalars()
    assert list(schemas) == ["finance"]
    version = owner_conn.execute(
        text("SELECT version_num FROM finance.alembic_version_finance")
    ).scalar_one()
    assert version == test_database.head == db.head_revision()


def test_every_owner_relation_is_in_schema_finance(owner_conn: Connection) -> None:
    outside = owner_conn.execute(
        text(
            "SELECT n.nspname || '.' || c.relname FROM pg_class c"
            " JOIN pg_namespace n ON n.oid = c.relnamespace"
            " WHERE pg_get_userbyid(c.relowner) = 'finance_owner'"
            " AND c.relkind IN ('r', 'p', 'v', 'm', 'S', 'f')"
            " AND n.nspname <> 'finance'"
        )
    ).scalars()
    assert list(outside) == []
    in_public = owner_conn.execute(
        text("SELECT count(*) FROM pg_tables WHERE schemaname = 'public'")
    )
    assert in_public.scalar_one() == 0


def test_env_py_falls_back_to_the_owner_dsn_in_db_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    test_database: DatabaseUnderTest,
) -> None:
    dsn = test_database.owner_url.set(drivername="postgresql").render_as_string(
        hide_password=False
    )
    env_file = tmp_path / db.ENV_FILE_NAME
    env_file.write_text(f"{db.OWNER_DSN_KEY}={dsn}\n", encoding="utf-8")
    env_file.chmod(0o600)
    monkeypatch.setenv("FINANCE_DATA_DIR", str(tmp_path))

    config = db.alembic_config()
    assert "connection" not in config.attributes
    command.upgrade(config, "head")  # no connection passed: env.py opens its own


# --- finance db up: credentials and the shared compose names ------------------------


def test_ensure_env_file_writes_private_credentials_once(tmp_path: Path) -> None:
    data = tmp_path / "Finances"
    path, created, _notes = db.ensure_env_file(data)
    assert created
    assert stat.S_IMODE(data.stat().st_mode) == 0o700
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    values = db.read_env_file(path)
    assert set(values) == {db.PASSWORD_KEY, db.OWNER_DSN_KEY, db.READER_DSN_KEY}
    assert all(values.values())
    for key, role in (
        (db.OWNER_DSN_KEY, "finance_owner"),
        (db.READER_DSN_KEY, "finance_reader"),
    ):
        url = db.to_sqlalchemy_url(values[key])
        assert (url.username, url.host, url.port, url.database) == (
            role,
            "127.0.0.1",
            55440,
            "finance",
        )
        assert url.password and url.password != values[db.PASSWORD_KEY]

    original = path.read_bytes()
    path.chmod(0o644)
    _path, created_again, notes = db.ensure_env_file(data)
    assert not created_again
    assert path.read_bytes() == original
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert any("0600" in note for note in notes)


def test_compose_pins_one_shared_container_and_volume(tmp_path: Path) -> None:
    # Any checkout (any project directory) must resolve to the same names.
    (tmp_path / db.ENV_FILE_NAME).write_text(
        "POSTGRES_PASSWORD=fake\nFINANCE_OWNER_DSN=fake\nFINANCE_READER_DSN=fake\n",
        encoding="utf-8",
    )
    result = subprocess.run(
        [
            "docker", "compose", "-f", str(db.COMPOSE_FILE),
            "--project-directory", str(tmp_path), "config", "--format", "json",
        ],
        capture_output=True,
        text=True,
        check=True,
        env=db.compose_env(tmp_path),
        cwd=tmp_path,
    )  # fmt: skip
    config = json.loads(result.stdout)
    assert config["name"] == db.COMPOSE_PROJECT
    service = config["services"]["postgres"]
    assert service["container_name"] == db.CONTAINER
    assert re.fullmatch(r"postgres:17\.\d+-alpine", service["image"])
    assert [(p["host_ip"], p["published"], p["target"]) for p in service["ports"]] == [
        ("127.0.0.1", "55440", 5432)
    ]
    assert config["volumes"]["finance-pgdata"]["name"] == "finance-pgdata"
    assert [v["source"] for v in service["volumes"]] == ["finance-pgdata"]


def test_finance_db_needs_a_subcommand(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["db"]) == 2
    assert "<db command>" in capsys.readouterr().err
