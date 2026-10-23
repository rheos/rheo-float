"""The guard around the drop-and-recreate of ``finance_test`` (``conftest``).

The first tests use a recording fake for ``db.psql``, ``db.init``, ``db.migrate`` and
the session lock, and fake ``db.env`` URLs, so nothing reaches a server. A database
that is not the container's own ``*_test`` database is refused before any call; a
stale chain stamp drops ``finance_test`` and nothing else, under the lock held alone.

The last two run the real advisory lock against throwaway ``*_test`` databases: a
rebuild waits for a session still using the database, and two sessions that find
the same stale stamp rebuild it once.
"""

from __future__ import annotations

import secrets
import threading
import time
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.pool import NullPool

import conftest
from rheo_finance import db

ENV_OWNER = "postgresql+psycopg://finance_owner:owner-pw@127.0.0.1:55440/finance"
ENV_READER = "postgresql+psycopg://finance_reader:reader-pw@127.0.0.1:55440/finance"
WAIT_SECONDS = 120.0


@dataclass
class Recorder:
    """Every call the guard makes, in order, and the stamps the fake psql returns
    (the last one repeats)."""

    stamps: list[str] = field(default_factory=lambda: [""])
    calls: list[tuple[str, object]] = field(default_factory=list)

    def psql(
        self, sql: str, *, database: str = "postgres", hidden: Sequence[str] = ()
    ) -> str:
        self.calls.append(("psql", sql))
        if "shobj_description" not in sql:
            return ""
        return self.stamps.pop(0) if len(self.stamps) > 1 else self.stamps[0]

    def init(self, databases: Sequence[str] | None = None) -> list[str]:
        self.calls.append(("init", tuple(databases or ())))
        return []

    def migrate(self, url: URL | None = None) -> str:
        self.calls.append(("migrate", url.database if url else None))
        return "head"

    def share(self) -> None:
        self.calls.append(("lock", "share"))

    def own(self) -> None:
        self.calls.append(("lock", "own"))


@pytest.fixture
def recorder(monkeypatch: pytest.MonkeyPatch) -> Recorder:
    fake = Recorder()
    monkeypatch.setattr(db, "psql", fake.psql)
    monkeypatch.setattr(db, "init", fake.init)
    monkeypatch.setattr(db, "migrate", fake.migrate)

    def env_url(dsn: str) -> Callable[[str | None], URL]:
        def build(database: str | None = None) -> URL:
            url = make_url(dsn)
            return url.set(database=database) if database is not None else url

        return build

    monkeypatch.setattr(db, "owner_url", env_url(ENV_OWNER))
    monkeypatch.setattr(db, "reader_url", env_url(ENV_READER))
    return fake


def urls(
    database: str, *, host: str | None = None, password: str | None = None
) -> tuple[URL, URL]:
    owner = make_url(ENV_OWNER).set(database=database)
    if host is not None:
        owner = owner.set(host=host)
    if password is not None:
        owner = owner.set(password=password)
    return owner, make_url(ENV_READER).set(database=database)


def current() -> str:
    return conftest.CHAIN_STAMP + conftest.migration_chain_digest()


STALE = conftest.CHAIN_STAMP + "0" * 64


@pytest.mark.parametrize(
    "name", ["finance", 'finance"; DROP DATABASE finance; --_test']
)
def test_a_live_or_quoted_name_is_refused_with_no_call(
    recorder: Recorder, name: str
) -> None:
    owner, reader = urls(name)

    with pytest.raises(conftest.UnsafeTestDatabase):
        conftest._fresh_test_database(owner, reader, recorder)

    assert recorder.calls == []


@pytest.mark.parametrize(
    ("host", "password"), [("db.example.test", None), (None, "another-pw")]
)
def test_urls_that_are_not_the_container_s_own_are_left_alone(
    recorder: Recorder, host: str | None, password: str | None
) -> None:
    owner, reader = urls(db.TEST_DATABASE, host=host, password=password)

    conftest._fresh_test_database(owner, reader, recorder)

    assert recorder.calls == []


def test_a_stale_stamp_drops_and_rebuilds_only_finance_test_holding_the_lock_alone(
    recorder: Recorder,
) -> None:
    recorder.stamps = [STALE]
    owner, reader = urls(db.TEST_DATABASE)

    conftest._fresh_test_database(owner, reader, recorder)

    kinds = [kind if kind != "lock" else str(what) for kind, what in recorder.calls]
    assert kinds == [
        "share", "psql", "own", "psql", "psql", "init", "migrate", "psql", "share"
    ]  # fmt: skip
    drops = [sql for kind, sql in recorder.calls if "DROP" in str(sql)]
    assert drops == ['DROP DATABASE IF EXISTS "finance_test" WITH (FORCE);']
    assert ("init", ("finance_test",)) in recorder.calls
    assert ("migrate", "finance_test") in recorder.calls
    comment = str(recorder.calls[-2][1])
    assert comment.startswith('COMMENT ON DATABASE "finance_test" IS ')
    assert conftest.migration_chain_digest() in comment


def test_a_current_stamp_changes_nothing_and_keeps_the_lock_shared(
    recorder: Recorder,
) -> None:
    recorder.stamps = [current()]
    owner, reader = urls(db.TEST_DATABASE)

    conftest._fresh_test_database(owner, reader, recorder)

    assert recorder.calls[0] == ("lock", "share")
    assert [kind for kind, _ in recorder.calls] == ["lock", "psql"]


def test_a_stamp_rebuilt_while_waiting_for_the_lock_is_not_dropped_again(
    recorder: Recorder,
) -> None:
    recorder.stamps = [STALE, current()]
    owner, reader = urls(db.TEST_DATABASE)

    conftest._fresh_test_database(owner, reader, recorder)

    assert not [sql for _, sql in recorder.calls if "DROP" in str(sql)]
    assert recorder.calls[-1] == ("lock", "share")


# --- the real advisory lock -----------------------------------------------------------


@pytest.fixture
def throwaway(test_database: conftest.DatabaseUnderTest) -> Iterator[tuple[URL, URL]]:
    """A fresh ``*_test`` database with no chain stamp, dropped after the test."""
    name = f"finance_lock_{secrets.token_hex(4)}_test"
    owner = test_database.owner_url.set(database=name)
    reader = test_database.reader_url.set(database=name)
    if not conftest._container_owned(owner, reader):
        pytest.fail(
            "the lock tests rebuild a database, so the test DSNs must come from db.env",
            pytrace=False,
        )
    try:
        db.init(databases=(name,))
        yield owner, reader
    finally:
        db.psql(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE);')


def wait_for_lock_waiters(name: str, count: int) -> None:
    """Return once ``count`` sessions wait on ``name``'s advisory lock."""
    engine = create_engine(db.owner_url("postgres"), poolclass=NullPool)
    deadline = time.monotonic() + WAIT_SECONDS
    try:
        with engine.connect() as conn:
            while time.monotonic() < deadline:
                waiting = conn.execute(
                    text(
                        "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory'"
                        f" AND NOT granted AND classid = {conftest.LOCK_NAMESPACE}"
                        " AND objid = (hashtext(:name)::bigint & 4294967295)::oid"
                    ),
                    {"name": name},
                ).scalar_one()
                if waiting >= count:
                    return
                time.sleep(0.05)
    finally:
        engine.dispose()
    pytest.fail(f"no {count} lock waiters within {WAIT_SECONDS} s", pytrace=False)


def rebuild_in_thread(
    owner: URL, reader: URL, errors: list[BaseException], *, then_close: bool
) -> tuple[threading.Thread, conftest.AdvisoryLock]:
    lock = conftest.AdvisoryLock(owner, owner.database or "")

    def run() -> None:
        try:
            conftest._fresh_test_database(owner, reader, lock)
        except BaseException as exc:  # handed back to the test thread
            errors.append(exc)
        finally:
            if then_close:
                lock.close()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread, lock


def test_a_rebuild_waits_until_a_session_using_the_database_lets_go(
    throwaway: tuple[URL, URL],
) -> None:
    owner, reader = throwaway
    name = owner.database or ""
    running = conftest.AdvisoryLock(owner, name)
    running.share()
    engine = create_engine(owner, poolclass=NullPool)
    conn = engine.connect()
    errors: list[BaseException] = []
    thread, lock = rebuild_in_thread(owner, reader, errors, then_close=False)
    try:
        wait_for_lock_waiters(name, 1)
        # The running session's connection survives: nothing was dropped under it.
        assert conn.execute(text("SELECT 1")).scalar_one() == 1
        assert conftest._chain_stamp(name) == ""
    finally:
        conn.close()
        engine.dispose()
        running.close()
    try:
        thread.join(WAIT_SECONDS)
        assert not thread.is_alive()
        assert errors == []
        assert conftest._chain_stamp(name) == current()
    finally:
        lock.close()


def test_two_sessions_on_one_stale_stamp_rebuild_it_once(
    throwaway: tuple[URL, URL], monkeypatch: pytest.MonkeyPatch
) -> None:
    owner, reader = throwaway
    name = owner.database or ""
    drops: list[str] = []
    real_psql = db.psql

    def counting_psql(
        sql: str, *, database: str = "postgres", hidden: Sequence[str] = ()
    ) -> str:
        if sql.startswith("DROP DATABASE"):
            drops.append(sql)
        return real_psql(sql, database=database, hidden=hidden)

    monkeypatch.setattr(db, "psql", counting_psql)
    # A session still running holds both newcomers at the stale stamp, so each has
    # read it before either can rebuild.
    running = conftest.AdvisoryLock(owner, name)
    running.share()
    errors: list[BaseException] = []
    started = [rebuild_in_thread(owner, reader, errors, then_close=True)]
    started.append(rebuild_in_thread(owner, reader, errors, then_close=True))
    try:
        wait_for_lock_waiters(name, 2)
    finally:
        running.close()
    for thread, _ in started:
        thread.join(WAIT_SECONDS)
        assert not thread.is_alive()
    assert errors == []
    assert len(drops) == 1
    assert conftest._chain_stamp(name) == current()
