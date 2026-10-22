"""Views live in frozen SQL in their revisions, and the code still compiles to it.

A revision that creates or recreates a view holds its body in a module-level ``VIEWS``
dict. For every view a code module compiles (``sinks.views.VIEW_SELECTS``), the newest
revision's body must equal what the code compiles to now, whitespace aside. So a rule
changed in code, ``reversed_clause`` included, fails here until a new revision
recreates the view with the new body; and a revision can never change under a
database that already ran it.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

from rheo_finance import migrations
from rheo_finance.sinks.views import VIEW_SELECTS, view_sql

VERSIONS = Path(migrations.__file__).resolve().parent / "versions"


def _load(path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(f"_revision_{path.stem}", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _chain() -> list[ModuleType]:
    """Every revision, oldest first, by following ``down_revision``."""
    modules = [_load(path) for path in sorted(VERSIONS.glob("[0-9]*.py"))]
    by_parent = {module.down_revision: module for module in modules}
    assert len(by_parent) == len(modules), "the chain branches"
    chain: list[ModuleType] = []
    parent = None
    while parent in by_parent:
        chain.append(by_parent[parent])
        parent = chain[-1].revision
    assert len(chain) == len(modules), "a revision is off the chain"
    return chain


def newest_frozen_views() -> dict[str, str]:
    frozen: dict[str, str] = {}
    for module in _chain():
        frozen.update(getattr(module, "VIEWS", {}))
    return frozen


def normalized(sql: str) -> str:
    return " ".join(sql.split())


def test_every_code_view_has_a_frozen_body_in_a_revision() -> None:
    assert set(VIEW_SELECTS) <= set(newest_frozen_views())


def test_the_code_compiles_to_the_newest_frozen_body() -> None:
    frozen = newest_frozen_views()
    for name, build in VIEW_SELECTS.items():
        assert normalized(view_sql(build())) == normalized(frozen[name]), (
            f"{name}: the code no longer compiles to the newest revision's SQL;"
            " add a revision that recreates the view"
        )


def test_no_revision_imports_application_code() -> None:
    # A revision that compiled its SQL from live code would change under a database
    # that already ran it.
    for path in VERSIONS.glob("[0-9]*.py"):
        source = path.read_text(encoding="utf-8")
        assert "from rheo_finance" not in source, path.name
        assert "import rheo_finance" not in source, path.name
