"""Database fixtures: ``finance_test`` migrated once per session, a rolled-back owner
connection per test, and a reader engine.

DSNs come from ``FINANCE_TEST_OWNER_DSN`` and ``FINANCE_TEST_READER_DSN`` when both are
set, else from ``$FINANCE_DATA_DIR/db.env`` re-targeted at ``finance_test``. A database
that cannot be reached fails every test that needs it, loudly and with the command to
run. It never skips: a skipped database test passes vacuously.
"""

from __future__ import annotations

import datetime as dt
import os
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest
from sqlalchemy import Connection, Engine, create_engine, text
from sqlalchemy.engine import URL
from sqlalchemy.exc import OperationalError
from sqlalchemy.pool import NullPool

from rheo_finance import db, operations
from rheo_finance.clock import FakeClock
from rheo_finance.context import (
    FinanceContext,
    FinancePaths,
    LineSnapshot,
    UnitOfWork,
)
from rheo_finance.contracts import Role
from rheo_finance.settings import default_settings

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
