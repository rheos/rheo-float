"""Every module that registers a sink provider or preview builder is one
``sinks.store`` imports (``PROVIDER_MODULES``), so derive, the gate's callers and the
sink operations never miss a sink's spec.

The scan reads every module under ``src/rheo_finance``: an assignment into
``SINK_SPECS[...]`` or ``PREVIEW_BUILDERS[...]`` (a plain name or an attribute), or an
``update`` or ``setdefault`` call on either, counts as registering.
"""

from __future__ import annotations

import ast
from pathlib import Path

from rheo_finance.sinks.store import PROVIDER_MODULES

from .source_tree import PACKAGE, copy_package, modules

REGISTRIES = ("SINK_SPECS", "PREVIEW_BUILDERS")


def _registry(node: ast.expr) -> bool:
    if isinstance(node, ast.Name):
        return node.id in REGISTRIES
    return isinstance(node, ast.Attribute) and node.attr in REGISTRIES


def registering_modules(root: Path = PACKAGE) -> set[str]:
    found: set[str] = set()
    for module in modules(root):
        for node in ast.walk(module.tree):
            targets: list[ast.expr] = []
            if isinstance(node, ast.Assign):
                targets = node.targets
            elif isinstance(node, ast.AugAssign | ast.AnnAssign):
                targets = [node.target]
            if any(
                isinstance(each, ast.Subscript) and _registry(each.value)
                for each in targets
            ) or (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in {"update", "setdefault"}
                and _registry(node.func.value)
            ):
                found.add(module.name)
    return found


def test_every_registering_module_is_a_provider_module() -> None:
    assert registering_modules() == set(PROVIDER_MODULES)


def test_a_planted_registration_is_found(tmp_path: Path) -> None:
    root = copy_package(tmp_path)
    (root / "rates" / "planted.py").write_text(
        "from rheo_finance.sinks import spec\n"
        "spec.SINK_SPECS['coinstats'] = provider\n",
        encoding="utf-8",
    )
    (root / "rates" / "planted_two.py").write_text(
        "from rheo_finance.sinks.preview import PREVIEW_BUILDERS\n"
        "PREVIEW_BUILDERS.update(coinstats=builder)\n",
        encoding="utf-8",
    )
    assert registering_modules(root) - set(PROVIDER_MODULES) == {
        "rheo_finance.rates.planted",
        "rheo_finance.rates.planted_two",
    }
