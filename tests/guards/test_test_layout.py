"""No test module or conftest imports a fixture from another module under ``tests/``,
and the fixtures the directories share resolve under any file order.

A fixture imported from ``test_a.py`` into ``test_b.py`` is registered in both, so
what a test finds rests on what its module imports rather than on its directory. A
phase gate once saw a derive test report its ``ledger`` fixture as not found under one
file order; that failure did not reproduce later, so this guard removes the arrangement
instead of pinning the symptom. A fixture more than one directory uses lives in
``tests/conftest.py``, one directory's in that directory's ``conftest.py``.

The static scan parses every ``tests/**/*.py``, collects the functions each decorates
with ``pytest.fixture``, and checks every import of a module under ``tests/``: relative
(``from .test_a``), rooted at ``tests`` (``from tests.derive.test_a``) or at the
``tests`` directory on ``sys.path`` (``from derive.conftest``). Helpers, types and
constants may still be imported.

The order test runs pytest's ``--setup-plan`` in a subprocess, which resolves every
fixture and runs no test and no fixture, over files from three directories, forwards
and backwards. It needs no database.
"""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

import pytest

TESTS = Path(__file__).resolve().parents[1]
ROOT = TESTS.parent
# A top-level file first, and users of the shared fixtures in two directories.
ORDER = (
    "tests/test_settings.py",
    "tests/rates/test_fetch.py",
    "tests/derive/test_tuned_defaults.py",
    "tests/derive/test_conversions.py",
)
SUBPROCESS_SECONDS = 300


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


def _source(root: Path, path: Path, node: ast.ImportFrom) -> Path | None:
    """The file under ``root`` that ``node``, in ``path``, imports from, or None when
    it imports from anywhere else (the package under test, a library)."""
    if node.module is None:
        return None
    parts = node.module.split(".")
    if node.level:
        base = path.parent
        for _ in range(node.level - 1):
            base = base.parent
    else:
        base = root
        if parts[0] == root.name:
            parts = parts[1:]
    found = base.joinpath(*parts).with_suffix(".py")
    return found if found.is_file() else None


def problems(root: Path) -> list[str]:
    """Every import, in a module under ``root``, of a fixture from another one."""
    trees = {
        path: ast.parse(path.read_text(encoding="utf-8"))
        for path in sorted(root.rglob("*.py"))
    }
    fixtures = {path: _fixtures(tree) for path, tree in trees.items()}
    found: list[str] = []
    for path, tree in trees.items():
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom):
                continue
            source = _source(root, path, node)
            if source is None:
                continue
            found.extend(
                f"{path.relative_to(root)} imports fixture {alias.name}"
                f" from {source.relative_to(root)}"
                for alias in node.names
                if alias.name in fixtures.get(source, set())
            )
    return found


def test_no_module_imports_a_fixture_from_another() -> None:
    assert problems(TESTS) == []


def test_the_scan_finds_each_way_of_importing_a_fixture(tmp_path: Path) -> None:
    root = tmp_path / "tests"
    (root / "derive").mkdir(parents=True)
    (root / "rates").mkdir()
    fixture = "import pytest\n\n@pytest.fixture\ndef own() -> int:\n    return 1\n"
    (root / "derive" / "conftest.py").write_text(fixture, encoding="utf-8")
    (root / "derive" / "test_shared.py").write_text(fixture, encoding="utf-8")
    written = {
        "derive/test_relative.py": "from .test_shared import own\n",
        "derive/test_rooted.py": "from tests.derive.test_shared import own\n",
        "rates/conftest.py": "from derive.conftest import own\n",
        "rates/test_path.py": "from derive.test_shared import own\n",
        "rates/test_helper.py": "from derive.test_shared import pytest\n",
        "rates/test_library.py": "from pytest import fixture\n",
    }
    for name, text in written.items():
        (root / name).write_text(text, encoding="utf-8")

    found = problems(root)

    assert found == [
        "derive/test_relative.py imports fixture own from derive/test_shared.py",
        "derive/test_rooted.py imports fixture own from derive/test_shared.py",
        "rates/conftest.py imports fixture own from derive/conftest.py",
        "rates/test_path.py imports fixture own from derive/test_shared.py",
    ]


@pytest.mark.parametrize(
    "order", [ORDER, tuple(reversed(ORDER))], ids=["forwards", "backwards"]
)
def test_every_fixture_resolves_under_either_file_order(
    order: tuple[str, ...],
) -> None:
    done = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
         "--setup-plan", *order],
        cwd=ROOT, capture_output=True, text=True, timeout=SUBPROCESS_SECONDS,
        check=False,
    )  # fmt: skip

    assert done.returncode == 0, done.stdout[-2000:]
