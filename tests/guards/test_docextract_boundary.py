"""FR-7, FR-47 and Key decision 1: ``docextract`` has one door.

Only ``rheo_finance.parsers.docextract_adapter`` imports ``docextract`` (an import, a
``from`` import, or a literal ``importlib.import_module`` / ``__import__`` name, at any
depth), and it imports only the names Key decision 1 lists: nothing from ``monitor``,
``docextract.extractor`` (the model step) or ``docextract.api``. ``scripts/`` is
scanned too.
"""

from __future__ import annotations

import ast
from pathlib import Path

from .source_tree import (
    PACKAGE,
    SCRIPTS,
    Module,
    calls,
    copy_package,
    imports,
    modules,
    names_module,
    plant,
)

ADAPTER = "rheo_finance.parsers.docextract_adapter"
ALLOWED = frozenset(
    {
        "docextract.vancity", "docextract.vancity.parse",
        "docextract.vancity.VancityParseError",
        "docextract.reference", "docextract.reference.parse",
        "docextract.pdftext", "docextract.pdftext.pdf_to_text",
        "docextract.pdftext.NotTextPDF",
        "docextract.schema", "docextract.schema.to_money",
    }
)  # fmt: skip
FORBIDDEN = ("monitor", "docextract")


def _named(module: Module) -> list[str]:
    found = [each.name for each in imports(module)]
    for name, call in calls(module):
        if name in ("importlib.import_module", "__import__") and call.args:
            first = call.args[0]
            if isinstance(first, ast.Constant) and isinstance(first.value, str):
                found.append(first.value)
    return found


def boundary_problems(*roots: Path) -> list[str]:
    problems: list[str] = []
    for root in roots:
        for module in modules(root):
            for name in _named(module):
                if not any(names_module(name, each) for each in FORBIDDEN):
                    continue
                if module.name != ADAPTER:
                    problems.append(f"{module.name} imports {name}")
                elif name not in ALLOWED and name != "docextract":
                    problems.append(
                        f"{module.name} imports {name}, not an allowed name"
                    )
    return sorted(problems)


def test_only_the_adapter_imports_docextract() -> None:
    assert boundary_problems(PACKAGE, SCRIPTS) == []
    adapter = next(each for each in modules(PACKAGE) if each.name == ADAPTER)
    assert {"docextract.vancity.parse", "docextract.pdftext.pdf_to_text"} <= set(
        _named(adapter)
    ), "the control: the adapter's imports are read"


def test_a_second_importer_and_a_forbidden_name_are_found(tmp_path: Path) -> None:
    root = copy_package(tmp_path)
    plant(root, "parsers/sneaky.py", "def f():\n    from docextract import vancity\n")
    plant(
        root,
        "jobs/dynamic.py",
        "import importlib\nimportlib.import_module('docextract.extractor')\n",
    )
    adapter = root / "parsers" / "docextract_adapter.py"
    adapter.write_text(
        adapter.read_text(encoding="utf-8")
        + "\nfrom docextract.extractor import extract\nimport monitor.virgin_monitor\n",
        encoding="utf-8",
    )
    assert boundary_problems(root) == [
        "rheo_finance.jobs.dynamic imports docextract.extractor",
        f"{ADAPTER} imports docextract.extractor, not an allowed name",
        f"{ADAPTER} imports docextract.extractor.extract, not an allowed name",
        f"{ADAPTER} imports monitor.virgin_monitor, not an allowed name",
        "rheo_finance.parsers.sneaky imports docextract",
        "rheo_finance.parsers.sneaky imports docextract.vancity",
    ]
