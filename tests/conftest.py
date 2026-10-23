"""Database fixtures: ``finance_test`` migrated once per session, a rolled-back owner
connection per test, and a reader engine.

Also the sink fixtures: a settings file under a temporary ``$FINANCE_DATA_DIR``, a
direct approval record, and a local stub HTTP server; and ``scratch_database``, a
throwaway database for the one kind of test that must commit rows (reading them back
under the reader DSN).

DSNs come from ``FINANCE_TEST_OWNER_DSN`` and ``FINANCE_TEST_READER_DSN`` when both are
set, else from ``$FINANCE_DATA_DIR/db.env`` re-targeted at ``finance_test``. A database
that cannot be reached fails every test that needs it, loudly and with the command to
run. It never skips: a skipped database test passes vacuously.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import os
import secrets
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlsplit

import pytest
from sqlalchemy import Connection, Engine, create_engine, insert, text
from sqlalchemy.engine import URL
from sqlalchemy.exc import OperationalError
from sqlalchemy.pool import NullPool

from rheo_finance import db, operations, settings
from rheo_finance.clock import FakeClock
from rheo_finance.context import (
    FinanceContext,
    FinancePaths,
    LineSnapshot,
    UnitOfWork,
)
from rheo_finance.contracts import Role
from rheo_finance.settings import parse_settings
from rheo_finance.storage import schema
from rheo_finance.storage.ids import uuid7

TEST_REMEDY = (
    "the test database is unreachable: run `uv run finance db up && uv run finance db"
    " init`, or set FINANCE_TEST_OWNER_DSN and FINANCE_TEST_READER_DSN"
)

# The exit code of the tripwire ``claude`` every test finds first on PATH.
CLAUDE_TRIPWIRE_EXIT = 97
_CLAUDE_TRIPWIRE = (
    "#!/bin/sh\n"
    'echo "the real claude is blocked in the test suite" >&2\n'
    f"exit {CLAUDE_TRIPWIRE_EXIT}\n"
)


@pytest.fixture(scope="session", autouse=True)
def claude_tripwire(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Path]:
    """No test can launch the owner's real ``claude``: a ``claude`` that fails loudly
    is first on PATH for the whole session. A model test replaces PATH with its own
    fake; anything else that looks ``claude`` up gets this one."""
    directory = tmp_path_factory.mktemp("claude-tripwire")
    script = directory / "claude"
    script.write_text(_CLAUDE_TRIPWIRE, encoding="utf-8")
    script.chmod(0o755)
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("PATH", f"{directory}{os.pathsep}{os.environ.get('PATH', '')}")
        yield directory


@dataclass(frozen=True)
class DatabaseUnderTest:
    owner_url: URL
    reader_url: URL
    head: str


def _test_urls() -> tuple[URL, URL]:
    owner = os.environ.get("FINANCE_TEST_OWNER_DSN", "").strip()
    reader = os.environ.get("FINANCE_TEST_READER_DSN", "").strip()
    try:
        if owner and reader:
            urls = (db.to_sqlalchemy_url(owner), db.to_sqlalchemy_url(reader))
        else:
            urls = (db.owner_url(db.TEST_DATABASE), db.reader_url(db.TEST_DATABASE))
    except db.DbError as exc:
        pytest.fail(f"{TEST_REMEDY} ({exc})", pytrace=False)
    for url in urls:
        # Tests migrate and write; never let them aim at the live ledger.
        if not (url.database or "").endswith("_test"):
            pytest.fail("test DSNs must name a database ending in _test", pytrace=False)
    return urls


CHAIN_STAMP = "migration chain sha256 "


def migration_chain_digest() -> str:
    """The sha256 over every revision file's name and bytes, in name order."""
    digest = hashlib.sha256()
    for path in sorted((db.MIGRATIONS_DIR / "versions").glob("*.py")):
        digest.update(path.name.encode() + b"\0" + path.read_bytes() + b"\0")
    return digest.hexdigest()


def _container_owned(owner: URL, reader: URL) -> bool:
    """True when the test URLs are the ``db.env`` ones for their database: the local
    container, whose superuser may drop and recreate it."""
    name = owner.database or ""
    try:
        expected = (db.owner_url(name), db.reader_url(name))
    except db.DbError:
        return False
    return tuple(
        url.render_as_string(hide_password=False) for url in expected
    ) == tuple(url.render_as_string(hide_password=False) for url in (owner, reader))


def _chain_stamp(name: str) -> str:
    return db.psql(
        "SELECT coalesce(shobj_description(oid, 'pg_database'), '')"
        f" FROM pg_database WHERE datname = '{name}';"
    ).strip()


class UnsafeTestDatabase(Exception):
    """A database the test fixtures must never drop or recreate."""


class BusyTestDatabase(Exception):
    """Another checkout's test session kept the test database past the lock wait."""


# Advisory lock key: this namespace and ``hashtext(<database name>)``.
LOCK_NAMESPACE = 0x46494E54
LOCK_WAIT = "30min"


class ChainLock(Protocol):
    """The lock a test session holds on the test database while it uses it.

    ``share`` holds it alongside other sessions on the same chain; ``own`` holds it
    alone, waiting until every other session has let go. Each call moves from the
    mode held to the one asked for."""

    def share(self) -> None: ...

    def own(self) -> None: ...


class AdvisoryLock:
    """A Postgres advisory lock on one test database's name, held on a connection to
    the server's ``postgres`` database: a lock taken inside the test database would
    end with its drop. Released when ``close`` runs or the process exits. The
    connection opens on first use, so a URL that is never locked is never dialled."""

    def __init__(self, server: URL, name: str) -> None:
        self._engine = create_engine(
            server.set(database="postgres"),
            poolclass=NullPool,
            isolation_level="AUTOCOMMIT",
        )
        self._name = name
        self._conn: Connection | None = None
        self._held: str | None = None

    def _call(self, function: str) -> None:
        if self._conn is None:
            self._conn = self._engine.connect()
            self._conn.execute(text(f"SET lock_timeout = '{LOCK_WAIT}'"))
        try:
            self._conn.execute(
                text(f"SELECT {function}({LOCK_NAMESPACE}, hashtext(:name))"),
                {"name": self._name},
            )
        except OperationalError as exc:
            if getattr(exc.orig, "sqlstate", None) == "55P03":
                raise BusyTestDatabase(
                    f"another test session held {self._name} for {LOCK_WAIT}: either"
                    " it was built from another migration chain, or it was rebuilding"
                    " the database for this chain and took longer than the wait; let"
                    " that suite finish first"
                ) from None
            raise

    def share(self) -> None:
        self._call("pg_advisory_lock_shared")
        if self._held == "exclusive":
            self._call("pg_advisory_unlock")
        self._held = "shared"

    def own(self) -> None:
        if self._held == "shared":
            self._call("pg_advisory_unlock_shared")
        self._call("pg_advisory_lock")
        self._held = "exclusive"

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None
        self._held = None
        self._engine.dispose()


def _fresh_test_database(owner: URL, reader: URL, lock: ChainLock) -> None:
    """Recreate the test database when it was migrated from other revision text, and
    leave ``lock`` held shared for as long as the caller uses the database.

    Alembic records only the revision id, so a revision whose text changed before any
    live database ran it (a re-pin on an unmerged branch) leaves ``finance_test`` with
    the old schema under the new id. The database's comment holds the digest of the
    chain that built it; when it differs, the database is dropped, set up as ``db
    init`` does and migrated again. Only a ``*_test`` database in the local container
    is ever dropped, never the live one. Test URLs that name another server are
    migrated as they stand, unlocked.

    Every session holds ``lock`` shared while it runs. A session that finds a stale
    stamp asks for it alone, so its drop waits until every other session on that
    database has finished, then checks the stamp again: a session on the same chain
    that waited behind another's rebuild finds it fresh and drops nothing.
    """
    name = owner.database or ""
    # Explicit raises, not asserts: ``python -O`` strips asserts, and these guard a
    # DROP DATABASE.
    if not name.endswith("_test") or name == db.LIVE_DATABASE:
        raise UnsafeTestDatabase("only a database whose name ends in _test is dropped")
    if not db.DATABASE_NAME.fullmatch(name):
        raise UnsafeTestDatabase("the test database name is not a plain name")
    if not _container_owned(owner, reader):
        return
    expected = CHAIN_STAMP + migration_chain_digest()
    lock.share()
    if _chain_stamp(name) == expected:
        return
    lock.own()
    if _chain_stamp(name) != expected:
        db.psql(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE);')
        db.init(databases=(name,))
        db.migrate(owner)
        db.psql(f"COMMENT ON DATABASE \"{name}\" IS '{expected}';")
    lock.share()


@pytest.fixture(scope="session")
def test_database() -> Iterator[DatabaseUnderTest]:
    """Migrate ``finance_test`` to head once for the whole session, recreating it
    first when it was built from other revision text. The session holds the
    database's advisory lock shared until it ends, so no other checkout drops the
    database under it."""
    owner, reader = _test_urls()
    lock = AdvisoryLock(owner, owner.database or "")
    try:
        try:
            _fresh_test_database(owner, reader, lock)
            head = db.migrate(owner)
        except BusyTestDatabase as exc:
            pytest.fail(str(exc), pytrace=False)
        except (db.DbError, OperationalError) as exc:
            pytest.fail(f"{TEST_REMEDY} ({exc})", pytrace=False)
        yield DatabaseUnderTest(owner_url=owner, reader_url=reader, head=head)
    finally:
        lock.close()


@pytest.fixture(scope="module")
def scratch_database(test_database: DatabaseUnderTest) -> Iterator[DatabaseUnderTest]:
    """A throwaway database, set up as ``db init`` does and migrated to head, dropped
    after the module.

    A reader connection cannot see rows inside another connection's rolled-back
    transaction, so a test that checks a view under the reader DSN commits its rows
    here: never in ``finance_test``, never in the live ledger. Values are synthetic.
    Module-scoped, so it is set up before any test's ``$FINANCE_DATA_DIR`` changes
    (``db init`` reads ``db.env`` from there).
    """
    name = f"finance_scratch_{secrets.token_hex(4)}_test"
    if name in (db.LIVE_DATABASE, db.TEST_DATABASE):
        raise UnsafeTestDatabase("a scratch database never takes a standing name")

    def drop() -> None:
        db.psql(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE);')

    owner = test_database.owner_url.set(database=name)
    reader = test_database.reader_url.set(database=name)
    # db init sets the cluster-wide role passwords from db.env. If the test URLs came
    # from FINANCE_TEST_*_DSN and name other roles, passwords or a server, init could
    # reset passwords the live database uses: stop before it runs.
    try:
        from_env_file = (db.owner_url(name), db.reader_url(name))
    except db.DbError as exc:
        pytest.fail(f"scratch database: cannot read db.env ({exc})", pytrace=False)
    if tuple(
        url.render_as_string(hide_password=False) for url in from_env_file
    ) != tuple(url.render_as_string(hide_password=False) for url in (owner, reader)):
        pytest.fail(
            "scratch database: db.env and the test DSNs (FINANCE_TEST_OWNER_DSN,"
            " FINANCE_TEST_READER_DSN) name different roles, passwords or servers;"
            " db init would set the role passwords from db.env, so it does not run",
            pytrace=False,
        )
    try:
        # Setup sits inside the cleanup scope: a database init created before a
        # failed migrate is dropped too.
        try:
            drop()
            db.init(databases=(name,))
            head = db.migrate(owner)
        except db.DbError as exc:
            pytest.fail(f"{TEST_REMEDY} (scratch database: {exc})", pytrace=False)
        yield DatabaseUnderTest(owner_url=owner, reader_url=reader, head=head)
    finally:
        drop()


@pytest.fixture(scope="session")
def owner_engine(test_database: DatabaseUnderTest) -> Iterator[Engine]:
    engine = create_engine(test_database.owner_url, poolclass=NullPool)
    yield engine
    engine.dispose()


@pytest.fixture
def owner_conn(owner_engine: Engine) -> Iterator[Connection]:
    """An owner connection inside a transaction that is rolled back after the test."""
    with owner_engine.connect() as connection:
        transaction = connection.begin()
        try:
            yield connection
        finally:
            transaction.rollback()


@pytest.fixture(scope="session")
def reader_engine(test_database: DatabaseUnderTest) -> Iterator[Engine]:
    engine = create_engine(test_database.reader_url, poolclass=NullPool)
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    except OperationalError as exc:
        pytest.fail(f"{TEST_REMEDY} (reader: {exc.orig})", pytrace=False)
    yield engine
    engine.dispose()


def no_snapshot(_uow: UnitOfWork, _ref: str) -> LineSnapshot | None:
    """The fixture snapshot provider: no line resolves."""
    return None


@pytest.fixture
def make_context(
    test_database: DatabaseUnderTest, tmp_path: Path
) -> Callable[[Role], FinanceContext]:
    """A ``FinanceContext`` for a role over ``finance_test``, with a fake clock."""

    # The archive and Downloads point at paths that do not exist, so an operation
    # dispatched with these settings never reads or writes the real directories.
    unset = parse_settings(
        f'[finance.paths]\narchive = "{tmp_path / "archive-unset"}"\n'
        f'downloads = "{tmp_path / "downloads-unset"}"\n'
    )

    def build(role: Role) -> FinanceContext:
        ctx = FinanceContext(
            settings=unset,
            clock=FakeClock(dt.datetime(2026, 1, 15, 12, 0, tzinfo=dt.UTC)),
            paths=FinancePaths(data=tmp_path),
            role=role,
            snapshot_provider=no_snapshot,
        )
        return operations.with_databases(
            ctx,
            owner_url=test_database.owner_url,
            reader_url=test_database.reader_url,
        )

    return build


@pytest.fixture
def rolled_back_context(
    owner_conn: Connection,
    make_context: Callable[[Role], FinanceContext],
) -> Callable[[Role], FinanceContext]:
    """``make_context``, but ``dispatch`` runs owner operations on ``owner_conn``.

    Each operation's commit releases a savepoint inside the test's transaction, which
    is rolled back after the test. The append-only tables keep committed rows for good,
    so a test that writes decisions dispatches through this.
    """

    def build(role: Role) -> FinanceContext:
        return operations.with_owner_connection(make_context(role), owner_conn)

    return build


@pytest.fixture
def empty_registry(
    monkeypatch: pytest.MonkeyPatch,
) -> dict[str, operations.OperationSpec]:
    """Swap ``operations.SPECS`` for an empty dict; the real one is restored after."""
    fresh: dict[str, operations.OperationSpec] = {}
    monkeypatch.setattr(operations, "SPECS", fresh)
    return fresh


# --- sinks: settings, a direct approval record, a local stub HTTP server -------------


@pytest.fixture
def sink_settings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Callable[..., None]:
    """Point ``$FINANCE_DATA_DIR`` at ``tmp_path`` (every sink setting false) and
    return ``set_enabled(sink=True, ...)``, which rewrites its ``settings.toml``.

    The gate reads settings from ``$FINANCE_DATA_DIR`` at each call, so without this
    a test would read the owner's real file. A sink setting is true only here.
    """
    monkeypatch.setenv("FINANCE_DATA_DIR", str(tmp_path))

    def set_enabled(**sinks: bool) -> None:
        lines = [
            f"[finance.sinks.{sink}]\nenabled = {str(on).lower()}\n"
            for sink, on in sinks.items()
        ]
        (tmp_path / settings.SETTINGS_FILE_NAME).write_text(
            "\n".join(lines), encoding="utf-8"
        )

    return set_enabled


@pytest.fixture
def insert_decision(owner_conn: Connection) -> Callable[..., uuid.UUID]:
    """Insert one decision row directly on ``owner_conn`` and return its id.

    ``finance.decision.record`` refuses ``sink_approve`` by design (only ``finance
    sink approve`` records one), so a test that needs an approval writes the row here,
    inside the test's rolled-back transaction. Pass ``at`` to order decisions.
    """

    def insert_row(kind: str, *, at: dt.datetime, **columns: object) -> uuid.UUID:
        decision_id = uuid7(at)
        owner_conn.execute(
            insert(schema.decision).values(
                id=decision_id, kind=kind, decided_at=at, **columns
            )
        )
        return decision_id

    return insert_row


@pytest.fixture
def record_sink_approval(
    insert_decision: Callable[..., uuid.UUID],
) -> Callable[..., uuid.UUID]:
    """Record a ``sink_approve`` for ``sink`` pinned to ``spec_digest``, at ``at``."""

    def approve(sink: str, spec_digest: str, *, at: dt.datetime) -> uuid.UUID:
        return insert_decision(
            "sink_approve",
            at=at,
            sink=sink,
            spec_digest=spec_digest,
            preview_digest="9" * 64,
        )

    return approve


@dataclass(frozen=True)
class StubRequest:
    method: str
    path: str
    headers: dict[str, str]
    body: bytes


@dataclass
class StubReply:
    status: int = 200
    headers: dict[str, str] = field(default_factory=dict)
    body: bytes = b"ok"
    delay: float = 0.0


class StubServer:
    """A local HTTP server on 127.0.0.1 and an ephemeral port that records every
    request. ``reply(path, ...)`` sets the answer for a path (query ignored);
    ``redirect(path, location)`` answers 302."""

    def __init__(self) -> None:
        self.requests: list[StubRequest] = []
        self._replies: dict[str, StubReply] = {}
        self._lock = threading.Lock()
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def _answer(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                with stub._lock:
                    stub.requests.append(
                        StubRequest(self.command, self.path, dict(self.headers), body)
                    )
                    reply = stub._replies.get(urlsplit(self.path).path, StubReply())
                if reply.delay:
                    time.sleep(reply.delay)
                self.send_response(reply.status)
                for name, value in reply.headers.items():
                    self.send_header(name, value)
                self.send_header("Content-Length", str(len(reply.body)))
                self.end_headers()
                self.wfile.write(reply.body)

            do_GET = do_POST = do_PATCH = do_PUT = do_DELETE = _answer

            def log_message(self, format: str, *args: object) -> None:
                pass  # keep test output quiet

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        self.port = int(self._server.server_address[1])
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            kwargs={"poll_interval": 0.02},
            daemon=True,
        )
        self._thread.start()

    @property
    def host(self) -> str:
        """The ``SinkSpec.hosts`` entry for this server."""
        return f"127.0.0.1:{self.port}"

    def url(self, path: str) -> str:
        return f"http://{self.host}{path}"

    def reply(self, path: str, **fields: Any) -> None:
        with self._lock:
            self._replies[path] = StubReply(**fields)

    def redirect(self, path: str, location: str) -> None:
        self.reply(path, status=302, headers={"Location": location}, body=b"")

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)


@pytest.fixture
def make_stub_server() -> Iterator[Callable[[], StubServer]]:
    """Start as many stub servers as a test needs; all stop after it."""
    started: list[StubServer] = []

    def start() -> StubServer:
        server = StubServer()
        started.append(server)
        return server

    yield start
    for server in started:
        server.stop()


@pytest.fixture
def stub_server(make_stub_server: Callable[[], StubServer]) -> StubServer:
    return make_stub_server()
