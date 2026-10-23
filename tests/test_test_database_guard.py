"""The guard around the drop-and-recreate of ``finance_test`` (``conftest``).

A recording fake stands in for ``db.psql``, ``db.init`` and ``db.migrate``, and fake
``db.env`` URLs stand in for the owner's file, so nothing here reaches a server. A
database that is not the container's own ``*_test`` database is refused before any
call; a stale chain stamp drops ``finance_test`` and nothing else.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

import pytest
from sqlalchemy.engine import URL, make_url

import conftest
from rheo_finance import db

ENV_OWNER = "postgresql+psycopg://finance_owner:owner-pw@127.0.0.1:55440/finance"
ENV_READER = "postgresql+psycopg://finance_reader:reader-pw@127.0.0.1:55440/finance"


@dataclass
class Recorder:
    """Every call the guard makes, in order, and the stamp the fake psql returns."""

    stamp: str = ""
    calls: list[tuple[str, object]] = field(default_factory=list)

    def psql(
        self, sql: str, *, database: str = "postgres", hidden: Sequence[str] = ()
    ) -> str:
        self.calls.append(("psql", sql))
        return self.stamp if "shobj_description" in sql else ""

    def init(self, databases: Sequence[str] | None = None) -> list[str]:
        self.calls.append(("init", tuple(databases or ())))
        return []

    def migrate(self, url: URL | None = None) -> str:
        self.calls.append(("migrate", url.database if url else None))
        return "head"


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


@pytest.mark.parametrize(
    "name", ["finance", 'finance"; DROP DATABASE finance; --_test']
)
def test_a_live_or_quoted_name_is_refused_with_no_call(
    recorder: Recorder, name: str
) -> None:
    owner, reader = urls(name)

    with pytest.raises(conftest.UnsafeTestDatabase):
        conftest._fresh_test_database(owner, reader)

    assert recorder.calls == []


@pytest.mark.parametrize(
    ("host", "password"), [("db.example.test", None), (None, "another-pw")]
)
def test_urls_that_are_not_the_container_s_own_are_left_alone(
    recorder: Recorder, host: str | None, password: str | None
) -> None:
    owner, reader = urls(db.TEST_DATABASE, host=host, password=password)

    conftest._fresh_test_database(owner, reader)

    assert recorder.calls == []


def test_a_stale_stamp_drops_and_rebuilds_only_finance_test(recorder: Recorder) -> None:
    recorder.stamp = conftest.CHAIN_STAMP + "0" * 64
    owner, reader = urls(db.TEST_DATABASE)

    conftest._fresh_test_database(owner, reader)

    kinds = [kind for kind, _ in recorder.calls]
    assert kinds == ["psql", "psql", "init", "migrate", "psql"]
    drops = [sql for kind, sql in recorder.calls if "DROP" in str(sql)]
    assert drops == ['DROP DATABASE IF EXISTS "finance_test" WITH (FORCE);']
    assert ("init", ("finance_test",)) in recorder.calls
    assert ("migrate", "finance_test") in recorder.calls
    comment = str(recorder.calls[-1][1])
    assert comment.startswith('COMMENT ON DATABASE "finance_test" IS ')
    assert conftest.migration_chain_digest() in comment


def test_a_current_stamp_changes_nothing(recorder: Recorder) -> None:
    recorder.stamp = conftest.CHAIN_STAMP + conftest.migration_chain_digest()
    owner, reader = urls(db.TEST_DATABASE)

    conftest._fresh_test_database(owner, reader)

    assert [kind for kind, _ in recorder.calls] == ["psql"]
