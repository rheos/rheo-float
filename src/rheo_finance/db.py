"""The local Postgres: ``finance db up``, ``finance db init``, ``finance db migrate``.

This module owns the ``docker compose`` and ``docker exec`` subprocess calls. It keeps
sqlalchemy, psycopg and alembic out of its top-level imports, because ``cli.main``
imports it to register the ``db`` command and must not load the database layer.

Credentials live in ``$FINANCE_DATA_DIR/db.env`` (mode 0600). Nothing here prints a
password or a DSN that carries one; subprocess errors are scrubbed before display.
"""

from __future__ import annotations

import argparse
import os
import re
import secrets
import stat
import subprocess
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from rheo_finance.render import Report, emit, emit_error

if TYPE_CHECKING:
    from alembic.config import Config
    from sqlalchemy.engine import URL

# Pinned so every checkout shares one container and one volume (see docker-compose.yml).
COMPOSE_PROJECT = "rheo-finance"
CONTAINER = "rheo-finance-postgres"
HOST = "127.0.0.1"
PORT = 55440
SUPERUSER = "postgres"

OWNER_ROLE = "finance_owner"
READER_ROLE = "finance_reader"
SCHEMA = "finance"
LIVE_DATABASE = "finance"
TEST_DATABASE = "finance_test"
DATABASES = (LIVE_DATABASE, TEST_DATABASE)
VERSION_TABLE = "alembic_version_finance"
# Names go into SQL as quoted identifiers, so only plain lower-case names are accepted.
DATABASE_NAME = re.compile(r"[a-z_][a-z0-9_]{0,62}")

ENV_FILE_NAME = "db.env"
PASSWORD_KEY = "POSTGRES_PASSWORD"
OWNER_DSN_KEY = "FINANCE_OWNER_DSN"
READER_DSN_KEY = "FINANCE_READER_DSN"

DEFAULT_DATA_DIR = "~/Finances"
READY_TIMEOUT_SECONDS = 180.0
COMMAND_TIMEOUT_SECONDS = 120.0
# Room for a first image pull. A pull that hangs (say, on a credential helper waiting
# for a keychain prompt) fails here instead of blocking the command forever.
COMPOSE_UP_TIMEOUT_SECONDS = 600.0

REPO_ROOT = Path(__file__).resolve().parents[2]
COMPOSE_FILE = REPO_ROOT / "docker-compose.yml"
MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"

REMEDY = "run `uv run finance db up && uv run finance db init`"


class DbError(Exception):
    """A database command failed. The message is safe to print: it holds no secret."""


@dataclass
class UpResult:
    env_file: Path
    env_file_created: bool
    notes: list[str] = field(default_factory=list)


# --- paths and credentials ---------------------------------------------------------


def data_dir() -> Path:
    """``$FINANCE_DATA_DIR``, default ``~/Finances``, with ``~`` expanded."""
    raw = os.environ.get("FINANCE_DATA_DIR", "").strip() or DEFAULT_DATA_DIR
    return Path(raw).expanduser().absolute()


def env_file_path(data: Path | None = None) -> Path:
    return (data or data_dir()) / ENV_FILE_NAME


def read_env_file(path: Path | None = None) -> dict[str, str]:
    """Parse ``KEY=VALUE`` lines; ``#`` comments and blank lines are skipped."""
    target = path or env_file_path()
    try:
        text = target.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise DbError(f"no {ENV_FILE_NAME} at {target}; {REMEDY}") from None
    values: dict[str, str] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        values[key.strip()] = value.strip()
    return values


def _env_value(values: dict[str, str], key: str, path: Path) -> str:
    value = values.get(key, "")
    if not value:
        raise DbError(f"{path} has no value for {key}")
    return value


def ensure_env_file(data: Path | None = None) -> tuple[Path, bool, list[str]]:
    """Create the data directory (0700) and ``db.env`` (0600) when missing.

    Returns the file path, whether it was created, and notes. An existing file is never
    rewritten; if it is readable by group or others, its mode is tightened to 0600.
    """
    directory = data or data_dir()
    notes: list[str] = []
    if not directory.exists():
        directory.mkdir(mode=0o700, parents=True)
        notes.append(f"created {directory} (mode 0700)")
    path = env_file_path(directory)
    if path.exists():
        mode = stat.S_IMODE(path.stat().st_mode)
        if mode & 0o077:
            path.chmod(0o600)
            notes.append(f"tightened {path} to mode 0600")
        return path, False, notes
    superuser_password = secrets.token_urlsafe(32)
    owner_password = secrets.token_urlsafe(32)
    reader_password = secrets.token_urlsafe(32)
    body = (
        "# Written by `finance db up`. Private: keep it mode 0600, never commit it.\n"
        f"{PASSWORD_KEY}={superuser_password}\n"
        f"{OWNER_DSN_KEY}=postgresql://{OWNER_ROLE}:{owner_password}"
        f"@{HOST}:{PORT}/{LIVE_DATABASE}\n"
        f"{READER_DSN_KEY}=postgresql://{READER_ROLE}:{reader_password}"
        f"@{HOST}:{PORT}/{LIVE_DATABASE}\n"
    )
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(body)
    return path, True, notes


def to_sqlalchemy_url(dsn: str, database: str | None = None) -> URL:
    """A libpq URI as a SQLAlchemy URL on psycopg 3, optionally for another database."""
    from sqlalchemy.engine import make_url

    url = make_url(dsn).set(drivername="postgresql+psycopg")
    if database is not None:
        url = url.set(database=database)
    return url


def owner_url(database: str | None = None) -> URL:
    path = env_file_path()
    return to_sqlalchemy_url(
        _env_value(read_env_file(path), OWNER_DSN_KEY, path), database
    )


def reader_url(database: str | None = None) -> URL:
    path = env_file_path()
    return to_sqlalchemy_url(
        _env_value(read_env_file(path), READER_DSN_KEY, path), database
    )


# --- subprocess calls ---------------------------------------------------------------


def _scrub(text: str, hidden: Sequence[str]) -> str:
    for value in hidden:
        if value:
            text = text.replace(value, "<redacted>")
    return text


def _run(
    args: Sequence[str],
    *,
    stdin: str | None = None,
    hidden: Sequence[str] = (),
    env: dict[str, str] | None = None,
    timeout: float = COMMAND_TIMEOUT_SECONDS,
) -> str:
    try:
        result = subprocess.run(
            list(args),
            input=stdin,
            capture_output=True,
            text=True,
            check=False,
            env=env,
            timeout=timeout,
        )
    except FileNotFoundError:
        raise DbError(f"{args[0]} is not on PATH") from None
    except subprocess.TimeoutExpired:
        raise DbError(
            f"`{' '.join(args[:3])}` did not finish within {int(timeout)} s"
        ) from None
    if result.returncode != 0:
        detail = _scrub((result.stderr or result.stdout).strip(), hidden)
        tail = "\n".join(detail.splitlines()[-8:])
        raise DbError(f"`{' '.join(args[:3])}` exited {result.returncode}: {tail}")
    return result.stdout


def psql(sql: str, *, database: str = "postgres", hidden: Sequence[str] = ()) -> str:
    """Run SQL as the container superuser over its local socket; return unaligned rows.

    The SQL goes in on stdin, never on the command line, and the terse error verbosity
    keeps statement text (which may hold a password literal) out of error output.
    """
    return _run(
        [
            "docker", "exec", "-i", CONTAINER,
            "psql", "-X", "-q", "-A", "-t",
            "-v", "ON_ERROR_STOP=1", "-v", "VERBOSITY=terse",
            "-U", SUPERUSER, "-d", database,
        ],
        stdin=sql,
        hidden=hidden,
    )  # fmt: skip


def _rows(output: str) -> list[str]:
    return [line for line in output.splitlines() if line]


def _literal(value: str) -> str:
    # standard_conforming_strings is on (default since 9.1): doubling quotes is enough.
    return "'" + value.replace("'", "''") + "'"


# --- finance db up -----------------------------------------------------------------


def compose_env(data: Path | None = None) -> dict[str, str]:
    env = dict(os.environ)
    env["FINANCE_DATA_DIR"] = str(data or data_dir())
    return env


def up(timeout: float = READY_TIMEOUT_SECONDS) -> UpResult:
    if not COMPOSE_FILE.is_file():
        raise DbError(f"no compose file at {COMPOSE_FILE}")
    data = data_dir()
    path, created, notes = ensure_env_file(data)
    _run(
        [
            "docker", "compose", "-p", COMPOSE_PROJECT,
            "-f", str(COMPOSE_FILE), "up", "-d",
        ],
        env=compose_env(data),
        timeout=COMPOSE_UP_TIMEOUT_SECONDS,
    )  # fmt: skip
    wait_ready(timeout)
    return UpResult(env_file=path, env_file_created=created, notes=notes)


def wait_ready(timeout: float = READY_TIMEOUT_SECONDS) -> None:
    """Wait until the server accepts TCP connections inside the container.

    TCP, not the socket: on first start the image runs a socket-only bootstrap server,
    which would report ready before the real one starts.
    """
    deadline = time.monotonic() + timeout
    while True:
        try:
            status = _run(
                ["docker", "inspect", "-f", "{{.State.Status}}", CONTAINER], timeout=30
            ).strip()
        except DbError:
            status = ""
        if status in ("exited", "dead"):
            raise DbError(
                f"container {CONTAINER} is {status}; see `docker logs {CONTAINER}`"
            )
        if status == "running":
            try:
                _run(
                    [
                        "docker", "exec", CONTAINER,
                        "pg_isready", "-q", "-h", "127.0.0.1", "-U", SUPERUSER,
                    ],
                    timeout=30,
                )  # fmt: skip
                return
            except DbError:
                pass
        if time.monotonic() >= deadline:
            raise DbError(f"Postgres was not ready within {int(timeout)} s")
        time.sleep(1.0)


# --- finance db init ---------------------------------------------------------------


def _privilege_snapshot(databases: Sequence[str]) -> list[str]:
    names = ", ".join(_literal(name) for name in databases)
    rows = _rows(
        psql(
            "SELECT datname, coalesce(datacl::text, '') FROM pg_database"
            f" WHERE datname IN ({names}) ORDER BY datname;"
        )
    )
    for database in databases:
        rows += [
            f"{database}|{row}"
            for row in _rows(
                psql(
                    "SELECT nspname, pg_get_userbyid(nspowner),"
                    " coalesce(nspacl::text, '') FROM pg_namespace"
                    " WHERE nspname IN ('public', 'finance') ORDER BY nspname;",
                    database=database,
                )
            )
        ]
        rows += [
            f"{database}|default|{row}"
            for row in _rows(
                psql(
                    "SELECT pg_get_userbyid(defaclrole), defaclnamespace::regnamespace,"
                    " defaclobjtype, coalesce(defaclacl::text, '') FROM pg_default_acl"
                    " ORDER BY 1, 2, 3;",
                    database=database,
                )
            )
        ]
    return rows


def _password_ok(dsn: str, database: str) -> bool:
    import psycopg
    from psycopg.conninfo import make_conninfo

    try:
        with psycopg.connect(make_conninfo(dsn, dbname=database, connect_timeout=10)):
            return True
    except psycopg.OperationalError as exc:
        if "password authentication failed" in str(exc):
            return False
        raise DbError(
            f"could not connect to {database} on {HOST}:{PORT}: {exc}"
        ) from None


def init(databases: Sequence[str] | None = None) -> list[str]:
    """Create the roles, databases and schema, and set their privileges. Idempotent.

    ``databases`` defaults to ``finance`` and ``finance_test``; the roles are
    cluster-wide whatever it names. Returns one line per change; an empty list means
    nothing changed. The superuser owns each database; ``finance_owner`` owns schema
    ``finance``; ``finance_reader`` gets LOGIN, CONNECT and USAGE on ``finance`` and
    nothing else. ``PUBLIC`` loses CONNECT and TEMPORARY on the databases, every
    privilege on schema ``public``, and EXECUTE on functions ``finance_owner`` creates,
    so no role but the superuser can create a table outside ``finance`` and the reader
    cannot call the owner's functions.
    """
    databases = DATABASES if databases is None else tuple(databases)
    if not databases:
        raise DbError("no database to initialise")
    for database in databases:
        if not DATABASE_NAME.fullmatch(database):
            raise DbError(f"not a plain database name: {database!r}")
    path = env_file_path()
    values = read_env_file(path)
    dsns = {
        OWNER_ROLE: _env_value(values, OWNER_DSN_KEY, path),
        READER_ROLE: _env_value(values, READER_DSN_KEY, path),
    }
    passwords: dict[str, str] = {}
    for role, dsn in dsns.items():
        url = to_sqlalchemy_url(dsn)
        if url.username != role or not url.password:
            raise DbError(
                f"{path}: the DSN for {role} must name that role and a password"
            )
        passwords[role] = url.password
    hidden = list(passwords.values())
    changes: list[str] = []

    roles = ", ".join(_literal(role) for role in passwords)
    existing_roles = set(
        _rows(psql(f"SELECT rolname FROM pg_roles WHERE rolname IN ({roles});"))
    )
    for role, password in passwords.items():
        if role not in existing_roles:
            psql(
                f"CREATE ROLE {role} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE"
                f" NOREPLICATION PASSWORD {_literal(password)};",
                hidden=hidden,
            )
            changes.append(f"created role {role}")

    names = ", ".join(_literal(name) for name in databases)
    existing_databases = set(
        _rows(psql(f"SELECT datname FROM pg_database WHERE datname IN ({names});"))
    )
    for database in databases:
        if database not in existing_databases:
            psql(f'CREATE DATABASE "{database}";')
            changes.append(f"created database {database}")

    before = _privilege_snapshot(databases)
    for database in databases:
        psql(
            f'REVOKE ALL ON DATABASE "{database}" FROM PUBLIC;\n'
            f'GRANT CONNECT, TEMPORARY ON DATABASE "{database}" TO {OWNER_ROLE};\n'
            f'GRANT CONNECT ON DATABASE "{database}" TO {READER_ROLE};\n'
        )
        has_schema = _rows(
            psql(
                f"SELECT 1 FROM pg_namespace WHERE nspname = '{SCHEMA}';",
                database=database,
            )
        )
        if not has_schema:
            psql(
                f"CREATE SCHEMA {SCHEMA} AUTHORIZATION {OWNER_ROLE};", database=database
            )
            changes.append(f"created schema {SCHEMA} in {database}")
        psql(
            f"ALTER SCHEMA {SCHEMA} OWNER TO {OWNER_ROLE};\n"
            "REVOKE ALL ON SCHEMA public FROM PUBLIC;\n"
            f"REVOKE ALL ON SCHEMA {SCHEMA} FROM PUBLIC;\n"
            f"GRANT USAGE ON SCHEMA {SCHEMA} TO {READER_ROLE};\n"
            # Global, not IN SCHEMA: a per-schema default can only add to the global
            # default, and the global default gives PUBLIC EXECUTE on new functions.
            f"ALTER DEFAULT PRIVILEGES FOR ROLE {OWNER_ROLE}"
            " REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC;\n",
            database=database,
        )
    if _privilege_snapshot(databases) != before:
        changes.append("set database and schema privileges")

    # A role made before db.env was regenerated keeps its old password: reset it so the
    # DSNs work. A role whose password works is left alone, so a rerun is a no-op.
    for role, dsn in dsns.items():
        if not _password_ok(dsn, databases[0]):
            psql(
                f"ALTER ROLE {role} PASSWORD {_literal(passwords[role])};",
                hidden=hidden,
            )
            changes.append(
                f"reset the password of role {role} to match {ENV_FILE_NAME}"
            )
    return changes


# --- finance db migrate ------------------------------------------------------------


def alembic_config() -> Config:
    from alembic.config import Config

    config = Config()
    config.set_main_option("script_location", str(MIGRATIONS_DIR))
    return config


def head_revision() -> str:
    from alembic.script import ScriptDirectory

    head = ScriptDirectory.from_config(alembic_config()).get_current_head()
    if head is None:
        raise DbError("the migration chain has no head")
    return head


def migrate(url: URL | None = None) -> str:
    """Upgrade to head under the owner role (``url``, default the live owner DSN)."""
    from alembic import command
    from sqlalchemy import create_engine
    from sqlalchemy.exc import OperationalError
    from sqlalchemy.pool import NullPool

    config = alembic_config()
    engine = create_engine(url or owner_url(), poolclass=NullPool)
    try:
        with engine.begin() as connection:
            config.attributes["connection"] = connection
            command.upgrade(config, "head")
    except OperationalError as exc:
        raise DbError(f"could not migrate: {exc.orig}; {REMEDY}") from None
    finally:
        engine.dispose()
    return head_revision()


# --- the command -------------------------------------------------------------------


def _fail(command: str, exc: DbError) -> int:
    emit_error(Report.from_message(f"finance db {command}: failed. {exc}"))
    return 1


def _cmd_up(_args: argparse.Namespace) -> int:
    try:
        result = up()
    except DbError as exc:
        return _fail("up", exc)
    headline = [
        f"finance db up: Postgres is ready on {HOST}:{PORT} (container {CONTAINER})."
    ]
    if result.env_file_created:
        headline.append(
            f"Wrote {result.env_file} (mode 0600) with generated passwords."
        )
    notes = [note[0].upper() + note[1:] + "." for note in result.notes]
    emit(Report(headline, notes))
    return 0


def _cmd_init(args: argparse.Namespace) -> int:
    try:
        changes = init(args.databases)
    except DbError as exc:
        return _fail("init", exc)
    if not changes:
        emit(Report(["finance db init: already set up, nothing changed."]))
        return 0
    emit(
        Report(
            [f"finance db init: {len(changes)} change(s)."],
            [f"- {change}" for change in changes],
        )
    )
    return 0


def _cmd_migrate(_args: argparse.Namespace) -> int:
    try:
        head = migrate()
    except DbError as exc:
        return _fail("migrate", exc)
    emit(Report([f"finance db migrate: database {LIVE_DATABASE} is at head ({head})."]))
    return 0


def add_parser(commands: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = commands.add_parser(
        "db",
        help="run the local Postgres: up, init, migrate",
        description="Run the ledger's local Postgres container and its schema.",
    )
    sub = parser.add_subparsers(
        dest="db_command", metavar="<db command>", required=True
    )
    sub.add_parser(
        "up", help=f"start the container on {HOST}:{PORT} and write db.env if missing"
    ).set_defaults(handler=_cmd_up)
    init_parser = sub.add_parser(
        "init", help="create the roles, databases and schema (idempotent)"
    )
    init_parser.add_argument(
        "--database",
        dest="databases",
        action="append",
        metavar="NAME",
        help=f"set up only this database (repeatable; default: {', '.join(DATABASES)})",
    )
    init_parser.set_defaults(handler=_cmd_init)
    sub.add_parser(
        "migrate", help="upgrade the live database to the newest migration"
    ).set_defaults(handler=_cmd_migrate)
