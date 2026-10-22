"""The declared stack (AC-55): Python 3.12 under uv, SQLAlchemy 2.x Core with psycopg 3,
Pydantic v2, ruff and mypy strict, and a suite that runs against a real Postgres 17.

The schema and version-table clauses of AC-55 live in ``test_db.py``; the fixture clause
is the repo check's manifest rule, run over this tree in ``test_repo_check.py``.
"""

from __future__ import annotations

import re
import tomllib
from typing import Any

from sqlalchemy import Connection, text

from guards.source_tree import (
    PACKAGE,
    PYPROJECT,
    UV_LOCK,
    imports,
    modules,
    names_module,
)


def project() -> dict[str, Any]:
    return tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))


def requirement(name: str, requirements: list[str]) -> str:
    """The one requirement string for ``name``, extras and markers included."""
    pattern = rf"{re.escape(name)}(\[|[<>=!~ ]|$)"
    found = [each for each in requirements if re.match(pattern, each)]
    assert len(found) == 1, (name, requirements)
    return found[0]


def locked_major(name: str) -> int:
    lock = tomllib.loads(UV_LOCK.read_text(encoding="utf-8"))
    versions = [p["version"] for p in lock["package"] if p["name"] == name]
    assert len(versions) == 1, (name, versions)
    return int(versions[0].split(".")[0])


def test_python_is_pinned_to_3_12() -> None:
    assert project()["project"]["requires-python"] == ">=3.12,<3.13"
    lock = tomllib.loads(UV_LOCK.read_text(encoding="utf-8"))
    assert lock["requires-python"] == "==3.12.*"


def test_runtime_dependencies_pin_the_majors() -> None:
    deps = project()["project"]["dependencies"]
    assert requirement("sqlalchemy", deps) == "sqlalchemy>=2"
    assert requirement("psycopg", deps) == "psycopg[binary]>=3"
    assert requirement("pydantic", deps) == "pydantic>=2"
    requirement("alembic", deps)
    # uv resolved them to those majors.
    assert locked_major("sqlalchemy") == 2
    assert locked_major("psycopg") == 3
    assert locked_major("pydantic") == 2


def test_ruff_and_mypy_strict_are_the_dev_tools() -> None:
    config = project()
    dev = config["dependency-groups"]["dev"]
    for tool in ("ruff", "mypy", "pytest"):
        requirement(tool, dev)
    assert config["tool"]["mypy"]["strict"] is True
    assert config["tool"]["mypy"]["python_version"] == "3.12"
    assert config["tool"]["ruff"]["target-version"] == "py312"


def test_storage_is_sqlalchemy_core_not_the_orm() -> None:
    orm = [
        (module.name, each.name)
        for module in modules(PACKAGE)
        for each in imports(module)
        if names_module(each.name, "sqlalchemy.orm")
    ]
    assert orm == []


def test_the_suite_runs_against_a_real_postgres_17(owner_conn: Connection) -> None:
    database, version = owner_conn.execute(
        text("SELECT current_database(), current_setting('server_version_num')::int")
    ).one()
    assert database == "finance_test"
    assert 170000 <= version < 180000
