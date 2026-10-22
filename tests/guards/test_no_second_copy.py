"""AC-7 (part), FR-7: the Vancity and Virgin parsing rules are imported, not copied.

Three checks over ``src/rheo_finance``:

- only the adapter imports ``docextract`` (``test_docextract_boundary`` owns the scan;
  this test runs it again so AC-7 reads as one place);
- the parsers registered for the Vancity statement and the Virgin bill take their
  reading from the adapter's ``vancity_statement`` and ``reference_bill``;
- no module defines a docextract parsing name (a function, class or module-level
  pattern of ``docextract.vancity``, ``reference``, ``pdftext`` or ``schema``), and no
  string in code carries a distinctive fragment of docextract's regular expressions.
  Docstrings are not code and are skipped. Each name and fragment is checked against
  docextract's own source, so the lists cannot go stale unnoticed.
"""

from __future__ import annotations

import ast
import importlib.util
from pathlib import Path

from rheo_finance.parsers.registry import parsers_for

from .source_tree import PACKAGE, Module, copy_package, imports, modules, plant
from .test_docextract_boundary import ADAPTER, boundary_problems

# Names docextract defines that a copy of its rules would define again.
NAMES: dict[str, tuple[str, ...]] = {
    "docextract.vancity": (
        "VancityParseError", "error_report_deadline", "SUMMARY_LINE",
        "ACCOUNT_HEADER", "TXN_START", "AMOUNT_TAIL", "PRINTED_DATE", "KNOWN_FEES",
    ),
    "docextract.reference": ("usage_lines",),
    "docextract.pdftext": ("pdf_to_text", "NotTextPDF"),
    "docextract.schema": ("to_money",),
}  # fmt: skip
# Fragments of docextract's regular expressions (and one layout anchor), as they
# appear in its source.
TOKENS: dict[str, tuple[str, ...]] = {
    "docextract.vancity": (
        r"STATEMENT PERIOD:\s+",
        r"OPENING BALANCE\s+(",
        r"#(\d{6,})",
        r"(?:- ?(?:OD)?)?",
        r"NON[- ]?SUFFICIENT",
        r"DISHONOU?RED",
    ),
    "docextract.reference": (
        "Please detach this portion",
        r"Please pay by\*",
        r"Total current charges\s+including taxes",
        r"Total megabytes\s+",
        r"Monthly Device Payment\(s\)",
        r"Mobile adjustments \(see following pages\)",
    ),
}


def _source(module: str) -> str:
    spec = importlib.util.find_spec(module)
    assert spec is not None and spec.origin, module
    return Path(spec.origin).read_text(encoding="utf-8")


def _defined(tree: ast.AST) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            found.add(node.name)
        elif isinstance(node, ast.Assign):
            found |= {each.id for each in node.targets if isinstance(each, ast.Name)}
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            found.add(node.target.id)
    return found


def _docstrings(tree: ast.AST) -> set[int]:
    """The ids of the docstring nodes of every module, class and function."""
    found: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(
            node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef
        ):
            body = node.body
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                found.add(id(body[0].value))
    return found


def _strings(module: Module) -> list[str]:
    skip = _docstrings(module.tree)
    return [
        node.value
        for node in ast.walk(module.tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in skip
    ]


def copy_problems(root: Path) -> list[str]:
    names = {name for group in NAMES.values() for name in group}
    tokens = [token for group in TOKENS.values() for token in group]
    problems: list[str] = []
    for module in modules(root):
        for name in sorted(_defined(module.tree) & names):
            problems.append(f"{module.name} defines {name}")
        strings = _strings(module)
        for token in tokens:
            if any(token in each for each in strings):
                problems.append(f"{module.name} carries {token!r}")
    return sorted(problems)


def test_the_lists_are_docextracts_own_names_and_fragments() -> None:
    for module, names in NAMES.items():
        defined = _defined(ast.parse(_source(module)))
        assert set(names) <= defined, (module, set(names) - defined)
    for module, tokens in TOKENS.items():
        source = _source(module)
        assert [token for token in tokens if token not in source] == [], module


def test_only_the_adapter_imports_docextract() -> None:
    assert boundary_problems(PACKAGE) == []


def test_the_vancity_and_virgin_parsers_read_through_the_adapter() -> None:
    expected = {
        ("vancity", "pdf"): f"{ADAPTER}.vancity_statement",
        ("virgin-plus", "pdf"): f"{ADAPTER}.reference_bill",
    }
    by_name = {module.name: module for module in modules(PACKAGE)}
    for (issuer, fmt), entry in expected.items():
        registered = parsers_for(issuer, fmt)
        assert registered, (issuer, fmt)
        for parser in registered:
            module = by_name[parser.parse.__module__]
            assert entry in {each.name for each in imports(module)}, parser.parser_id


def test_no_module_copies_the_parsing_rules() -> None:
    assert copy_problems(PACKAGE) == []


def test_a_copied_rule_is_found_and_a_docstring_is_not(tmp_path: Path) -> None:
    root = copy_package(tmp_path)
    plant(
        root,
        "parsers/copied.py",
        '"""Reads the bill up to "Please detach this portion"."""\n'
        "import re\n\n"
        "CHARGES = re.compile(\n"
        "    r'Total current charges\\s+including taxes\\s+(\\S+)'\n"
        ")\n\n"
        "def usage_lines(text):\n"
        '    """Like docextract\'s, with "NON[- ]?SUFFICIENT" in the docstring."""\n'
        "    return []\n",
    )
    assert copy_problems(root) == [
        "rheo_finance.parsers.copied carries 'Total current charges\\\\s+including"
        " taxes'",
        "rheo_finance.parsers.copied defines usage_lines",
    ]
