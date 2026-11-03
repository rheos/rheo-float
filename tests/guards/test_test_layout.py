"""No test module imports a fixture from another test module.

A fixture imported from ``test_a.py`` into ``test_b.py`` is registered in both, so
what a test finds rests on what its module imports rather than on its directory. A
phase gate once saw a derive test report its ``ledger`` fixture as not found under one
file order; that failure did not reproduce later, so this guard removes the arrangement
instead of pinning the symptom. A fixture more than one module uses lives in a
``conftest.py``, where pytest finds it for every file under that directory.

Static: every ``tests/**/test_*.py`` is parsed, the functions it decorates with
``pytest.fixture`` are collected, and every relative or ``tests.``-rooted import of a
test module is checked against them. Helpers and constants may still be imported.
"""

from __future__ import annotations

import ast
from pathlib import Path

TESTS = Path(__file__).resolve().parents[1]


def _is_fixture(decorator: ast.expr) -> bool:
    target = decorator.func if isinstance(decorator, ast.Call) else decorator
    if isinstance(target, ast.Attribute):
        return target.attr == "fixture"
    return isinstance(target, ast.Name) and target.id == "fixture"


def _fixtures(tree: ast.Module) -> set[str]:
    return {
        node.name
        for node in tree.body
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
        and any(_is_fixture(each) for each in node.decorator_list)
    }


def _imported_test_module(path: Path, node: ast.ImportFrom) -> Path | None:
    """The test module file ``node`` imports from, or None when it imports from
    anything else (a support module, a conftest, the package under test)."""
    if node.module is None:
        return None
    name = node.module.split(".")[-1]
    if not name.startswith("test_"):
        return None
    if node.level:
        base = path.parent
        for _ in range(node.level - 1):
            base = base.parent
        return base.joinpath(*node.module.split(".")).with_suffix(".py")
    parts = node.module.split(".")
    if parts[0] != "tests":
        return None
    return TESTS.joinpath(*parts[1:]).with_suffix(".py")


def test_no_test_module_imports_a_fixture_from_another() -> None:
    modules = sorted(TESTS.rglob("test_*.py"))
    assert modules, "no test modules found"
    trees = {path: ast.parse(path.read_text(encoding="utf-8")) for path in modules}
    fixtures = {path: _fixtures(tree) for path, tree in trees.items()}
    found = []
    for path, tree in trees.items():
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom):
                continue
            source = _imported_test_module(path, node)
            if source is None:
                continue
            for alias in node.names:
                if alias.name in fixtures.get(source.resolve(), set()):
                    found.append(
                        f"{path.relative_to(TESTS)} imports fixture {alias.name}"
                        f" from {source.relative_to(TESTS)}"
                    )
    assert found == [], "move these fixtures to a conftest.py: " + "; ".join(found)


def test_the_guard_sees_a_cross_module_fixture_import(tmp_path: Path) -> None:
    """The scan itself: a fixture in one module imported by another is found."""
    shared = tmp_path / "test_shared.py"
    shared.write_text(
        "import pytest\n\n@pytest.fixture\ndef own() -> int:\n    return 1\n",
        encoding="utf-8",
    )
    user = tmp_path / "test_user.py"
    user.write_text("from .test_shared import own\n", encoding="utf-8")
    (node,) = [
        each
        for each in ast.walk(ast.parse(user.read_text(encoding="utf-8")))
        if isinstance(each, ast.ImportFrom)
    ]
    source = _imported_test_module(user, node)
    assert source == shared
    assert "own" in _fixtures(ast.parse(shared.read_text(encoding="utf-8")))
