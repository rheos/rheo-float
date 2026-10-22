"""Database fixtures: ``finance_test`` migrated once per session, a rolled-back owner
connection per test, and a reader engine.

Also the sink fixtures: a settings file under a temporary ``$FINANCE_DATA_DIR``, a
direct approval record, and a local stub HTTP server.

DSNs come from ``FINANCE_TEST_OWNER_DSN`` and ``FINANCE_TEST_READER_DSN`` when both are
set, else from ``$FINANCE_DATA_DIR/db.env`` re-targeted at ``finance_test``. A database
that cannot be reached fails every test that needs it, loudly and with the command to
run. It never skips: a skipped database test passes vacuously.
"""

from __future__ import annotations

import datetime as dt
import os
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
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
from rheo_finance.settings import default_settings
from rheo_finance.storage import schema
from rheo_finance.storage.ids import uuid7

TEST_REMEDY = (
    "the test database is unreachable: run `uv run finance db up && uv run finance db"
    " init`, or set FINANCE_TEST_OWNER_DSN and FINANCE_TEST_READER_DSN"
)


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


@pytest.fixture(scope="session")
def test_database() -> DatabaseUnderTest:
    """Migrate ``finance_test`` to head once for the whole session."""
    owner, reader = _test_urls()
    try:
        head = db.migrate(owner)
    except db.DbError as exc:
        pytest.fail(f"{TEST_REMEDY} ({exc})", pytrace=False)
    return DatabaseUnderTest(owner_url=owner, reader_url=reader, head=head)


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

    def build(role: Role) -> FinanceContext:
        ctx = FinanceContext(
            settings=default_settings(),
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
