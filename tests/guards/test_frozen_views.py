"""Revisions are frozen, views live in frozen SQL in them, and the code still compiles
to that SQL.

Every revision file is pinned by the sha256 of its bytes in ``PINNED``. Editing a
revision a database may already have run fails here. A deliberate edit before the
revision is applied anywhere (a review fix on an unmerged phase branch, say) re-pins
its hash in the same change; a new revision adds its pin.

A revision that creates or recreates a view holds its body in a module-level ``VIEWS``
dict. For every view a code module compiles (``sinks.views.VIEW_SELECTS``), the newest
revision's body must equal what the code compiles to now, whitespace aside. So a rule
changed in code, ``reversed_clause`` included, fails here until a new revision
recreates the view with the new body.
"""

from __future__ import annotations

import hashlib
import importlib.util
from pathlib import Path
from types import ModuleType

from rheo_finance import migrations
from rheo_finance.sinks.views import VIEW_SELECTS, view_sql

VERSIONS = Path(migrations.__file__).resolve().parent / "versions"

PINNED = {
    "0001_finance_schema.py": (
        "0f3e7fdad37e31b8efbfade234d6c3d2bf44d9db10fae40d43b56e2a33e88f4d"
    ),
    "0002_decisions.py": (
        "d98e325011730c9aac5f5ca10c4f140bff8c552a020d17e7ee26840a8d99973f"
    ),
    "0003_sink_spec.py": (
        "edf559eb46725a3d1f0e388582537b2382211affff6baeab42757fa9a239e385"
    ),
    "0004_sink_views.py": (
        "570690ba75b1777b1c91cb4d4c5144de9206cbcaafc1dfca8405c3ee816bed76"
    ),
    "0005_accounts.py": (
        "60f7a37270474d8c54ed2247313391276ae4f69db3ee5bd3962f7da643e06cda"
    ),
    "0006_filed_years.py": (
        "a699171f54b3ba2c7bda6432ec20f0a4e9cec35daa5b8c3a8ee535fb2026720d"
    ),
    "0007_documents.py": (
        "52457353a3934c1487ba58e7f28b9a1062a7bfabc5398b44ce60ada249e3f17d"
    ),
    "0008_archive_skip_unreadable.py": (
        "6efcdf6ff35187de0e550654fc1f492dfbb68f24617800b90b11e3dc7de9ec37"
    ),
    "0009_parse_cache.py": (
        "241b99575f19c5892cabd1b0610dec391ab7de086ba1bef2bb89c026b9ff4dd0"
    ),
    "0010_txn.py": ("f3a389a558902f2877ef5f0c5d41829ff337df57030b9d1b9fddd156a7f9accb"),
}


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


def test_every_revision_file_is_pinned_and_unchanged() -> None:
    found = {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(VERSIONS.glob("[0-9]*.py"))
    }
    assert set(found) == set(PINNED), "pin every revision file, and only those"
    changed = sorted(name for name, digest in found.items() if PINNED[name] != digest)
    assert changed == [], (
        "a revision file changed; a database that ran it will not see the edit."
        " Add a new revision instead, or re-pin only if it was never applied."
    )
