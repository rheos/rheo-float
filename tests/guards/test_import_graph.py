"""Key decision 14 and AC-78(c): the interactive commands are reachable from
``cli.main`` only.

``rheo_finance.cli.approve`` (``finance sink approve``) and, from Phase 5,
``rheo_finance.cli.bulk`` (``finance confirm bulk``) may be imported by
``rheo_finance.cli.main`` and by no other module, so no job, sink, model step, mail,
rates, connector or feature module reaches them. Nothing may import ``cli.main``
either, or it would reach them through it. The scan reads every module's source: an
``import``, a ``from`` import (relative ones resolved), and a string naming the module
(an ``importlib`` call) all count.
"""

from __future__ import annotations

import ast
from collections.abc import Iterator
from pathlib import Path

import pytest

import rheo_finance

PACKAGE = Path(rheo_finance.__file__).resolve().parent
MAIN = "rheo_finance.cli.main"
INTERACTIVE = ("rheo_finance.cli.approve", "rheo_finance.cli.bulk")
# The packages named by Key decision 14; the test covers every module regardless.
FORBIDDEN_PACKAGES = (
    "jobs", "sinks", "model", "mail", "rates", "connectors", "features",
)  # fmt: skip


def _modules() -> Iterator[tuple[str, Path]]:
    for path in sorted(PACKAGE.rglob("*.py")):
        relative = path.relative_to(PACKAGE.parent).with_suffix("")
        yield ".".join(relative.parts).removesuffix(".__init__"), path


def _is_package(path: Path) -> bool:
    return path.name == "__init__.py"


def imported_names(module: str, path: Path) -> set[str]:
    """Every dotted module name ``module`` imports or names in a string."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    package = module if _is_package(path) else module.rpartition(".")[0]
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                parts = package.split(".")
                base_parts = parts[: len(parts) - (node.level - 1)]
                base = ".".join(base_parts + ([node.module] if node.module else []))
            else:
                base = node.module or ""
            found.add(base)
            found.update(f"{base}.{alias.name}" for alias in node.names)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            found.add(node.value)
    return found


def importers(target: str) -> set[str]:
    return {
        module
        for module, path in _modules()
        if module != target
        and any(
            name == target or name.startswith(target + ".")
            for name in imported_names(module, path)
        )
    }


def existing(name: str) -> bool:
    return any(module == name for module, _ in _modules())


@pytest.mark.parametrize("target", INTERACTIVE)
def test_only_cli_main_imports_an_interactive_command(target: str) -> None:
    # A command not in this tree yet (cli.bulk before Phase 5) has no importer at all;
    # once it exists, cli.main is its one importer.
    assert importers(target) == ({MAIN} if existing(target) else set())


def test_nothing_imports_cli_main() -> None:
    assert importers(MAIN) == set()


def test_the_forbidden_packages_hold_no_importer() -> None:
    # Implied by the test above; stated so a failure names the package.
    for target in INTERACTIVE:
        offenders = sorted(
            module
            for module in importers(target)
            if any(
                module.startswith(f"rheo_finance.{package}")
                for package in FORBIDDEN_PACKAGES
            )
        )
        assert offenders == [], target


@pytest.mark.parametrize(
    ("source", "module", "is_package"),
    [
        ("import rheo_finance.cli.approve\n", "rheo_finance.jobs.tick", False),
        ("from rheo_finance.cli import approve\n", "rheo_finance.jobs.tick", False),
        ("from rheo_finance.cli.approve import run\n", "rheo_finance.sinks.x", False),
        ("from ..cli import approve\n", "rheo_finance.sinks.x", False),
        ("from . import approve\n", "rheo_finance.cli.sink", False),
        ("from .approve import run\n", "rheo_finance.cli", True),
        (
            "import importlib\nimportlib.import_module('rheo_finance.cli.approve')\n",
            "rheo_finance.features.x",
            False,
        ),
    ],
)
def test_the_scan_catches_each_import_form(
    source: str, module: str, is_package: bool, tmp_path: Path
) -> None:
    path = tmp_path / ("__init__.py" if is_package else "x.py")
    path.write_text(source, encoding="utf-8")
    names = imported_names(module, path)
    assert "rheo_finance.cli.approve" in names


def test_cli_main_really_imports_approve() -> None:
    # The control: the scan finds the one importer that is allowed.
    path = PACKAGE / "cli" / "main.py"
    assert "rheo_finance.cli.approve" in imported_names(MAIN, path)
