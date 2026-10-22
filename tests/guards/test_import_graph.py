"""Key decision 14 and AC-78(c): the interactive commands are reachable from
``cli.main`` only.

``rheo_finance.cli.approve`` (``finance sink approve``) and, from Phase 5,
``rheo_finance.cli.bulk`` (``finance confirm bulk``) may be imported by
``rheo_finance.cli.main`` and by no other module, so no job, sink, model step, mail,
rates, connector or feature module reaches them. Nothing may import ``cli.main``
either, or it would reach them through it. The scan reads every module's source: an
``import``, a ``from`` import (relative ones resolved), and a string naming the module
(an ``importlib`` call) all count.

One step further down: the operation name ``finance.sink.approve`` (what a dispatch
call takes) and the handler ``sinks.ops.approve`` appear only in ``cli.approve`` (the
caller) and ``sinks.ops`` (where it is defined and registered); inside ``sinks.ops``
the handler is named only as the approve spec's ``handler``. And the kind
``sink_approve`` (a string holding it, or ``SINK_APPROVE``) appears, outside
docstrings, only in ``sinks.ops`` (the one writer) and the modules allowed below, so no
other module can insert an approval row raw.
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


def _from_base(node: ast.ImportFrom, module: str, path: Path) -> str:
    """The absolute module a ``from`` import names, relative levels resolved."""
    if not node.level:
        return node.module or ""
    package = module if _is_package(path) else module.rpartition(".")[0]
    parts = package.split(".")
    base_parts = parts[: len(parts) - (node.level - 1)]
    return ".".join(base_parts + ([node.module] if node.module else []))


def imported_names(module: str, path: Path) -> set[str]:
    """Every dotted module name ``module`` imports or names in a string."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = _from_base(node, module, path)
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


APPROVE_OPERATION = "finance.sink.approve"
OPS = "rheo_finance.sinks.ops"
APPROVE_ALLOWED = {"rheo_finance.cli.approve", OPS}


def _dotted(node: ast.expr) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        head = _dotted(node.value)
        return None if head is None else f"{head}.{node.attr}"
    return None


def reaches_the_approval(module: str, path: Path) -> list[str]:
    """How ``module`` names the approval: the operation name, or the ops handler."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    how: list[str] = []
    ops_aliases: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == OPS:
                    ops_aliases.add(alias.asname or OPS)
        elif isinstance(node, ast.ImportFrom):
            base = _from_base(node, module, path)
            for alias in node.names:
                if base == OPS and alias.name == "approve":
                    how.append("imports sinks.ops.approve")
                if f"{base}.{alias.name}" == OPS:
                    ops_aliases.add(alias.asname or alias.name)
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and node.value in {
            APPROVE_OPERATION,
            f"{OPS}.approve",
        }:
            how.append(f"names {node.value!r}")
        elif isinstance(node, ast.Attribute) and node.attr == "approve":
            owner = _dotted(node.value)
            if owner is not None and owner in ops_aliases:
                how.append(f"uses {owner}.approve")
    return how


def test_only_cli_approve_and_sinks_ops_name_the_approval() -> None:
    offenders = {
        module: how
        for module, path in _modules()
        if module not in APPROVE_ALLOWED and (how := reaches_the_approval(module, path))
    }
    assert offenders == {}


def test_the_approval_scan_finds_the_allowed_ones() -> None:
    # The control: the caller and the definition are found, so the scan is not blind.
    found = {
        module: reaches_the_approval(module, path)
        for module, path in _modules()
        if module in APPROVE_ALLOWED
    }
    assert found["rheo_finance.cli.approve"] == ["names 'finance.sink.approve'"]
    assert "names 'finance.sink.approve'" in found[OPS]


@pytest.mark.parametrize(
    "source",
    [
        "from rheo_finance import operations\n"
        "operations.dispatch('finance.sink.approve', {}, role, ctx=ctx)\n",
        "from rheo_finance.sinks.ops import approve\n",
        "from ..sinks.ops import approve\n",
        "from rheo_finance.sinks import ops\nops.approve(ctx, uow, data)\n",
        "from rheo_finance.sinks import ops as o\no.approve(ctx, uow, data)\n",
        "import rheo_finance.sinks.ops as o\no.approve(ctx, uow, data)\n",
        "import rheo_finance.sinks.ops\nrheo_finance.sinks.ops.approve(c, u, d)\n",
        "resolve('rheo_finance.sinks.ops.approve')\n",
    ],
)
def test_the_approval_scan_catches_each_form(source: str, tmp_path: Path) -> None:
    path = tmp_path / "x.py"
    path.write_text(source, encoding="utf-8")
    assert reaches_the_approval("rheo_finance.jobs.x", path) != []


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


# --- the sink_approve kind and the approve handler -----------------------------------

KIND = "sink_approve"
# Who may name the kind, and why. Everyone else is refused, readers included.
KIND_ALLOWED = {
    OPS: "the one writer of sink_approve rows",
    "rheo_finance.decision": "the kind enum, its column matrix and record's refusal",
    "rheo_finance.sinks.gate": "reads the newest sink decision's kind",
    "rheo_finance.sinks.views": "the report_sink select compares the kind",
    "rheo_finance.migrations.versions.0002_decisions": "the frozen kind CHECK",
    "rheo_finance.migrations.versions.0004_sink_views": "the frozen view SQL",
}


def _docstrings(tree: ast.Module) -> set[int]:
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


def names_the_kind(path: Path) -> bool:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    docs = _docstrings(tree)
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in docs
            and KIND in node.value
        ):
            return True
        if (isinstance(node, ast.Attribute) and node.attr == KIND.upper()) or (
            isinstance(node, ast.Name) and node.id == KIND.upper()
        ):
            return True
    return False


def test_only_the_allowed_modules_name_the_sink_approve_kind() -> None:
    naming = {module for module, path in _modules() if names_the_kind(path)}
    assert naming - set(KIND_ALLOWED) == set()
    # The control: the writer itself is found, so the scan is not blind.
    assert OPS in naming


@pytest.mark.parametrize(
    "source",
    [
        "insert(decision).values(kind='sink_approve', sink='mot')\n",
        "insert(decision).values(kind=DecisionKind.SINK_APPROVE.value)\n",
        "from rheo_finance.decision import K\nK.SINK_APPROVE\n",
        'uow.execute(text("INSERT INTO finance.decision (kind) VALUES'
        " ('sink_approve')\"))\n",
    ],
)
def test_the_kind_scan_catches_each_form(source: str, tmp_path: Path) -> None:
    path = tmp_path / "x.py"
    path.write_text(source, encoding="utf-8")
    assert names_the_kind(path)


def test_the_kind_scan_ignores_docstrings(tmp_path: Path) -> None:
    path = tmp_path / "x.py"
    path.write_text('"""Mentions sink_approve."""\n', encoding="utf-8")
    assert not names_the_kind(path)


def _handler_uses(tree: ast.Module) -> list[ast.Name]:
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Name)
        and node.id == "approve"
        and isinstance(node.ctx, ast.Load)
    ]


def _approve_spec_handlers(tree: ast.Module) -> list[ast.expr]:
    """The ``handler=`` value of each ``OperationSpec(name="finance.sink.approve")``."""
    found: list[ast.expr] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "OperationSpec"
        ):
            keywords = {keyword.arg: keyword.value for keyword in node.keywords}
            name = keywords.get("name")
            if (
                isinstance(name, ast.Constant)
                and name.value == APPROVE_OPERATION
                and "handler" in keywords
            ):
                found.append(keywords["handler"])
    return found


def test_inside_sinks_ops_the_approve_handler_is_named_only_by_its_spec() -> None:
    path = PACKAGE / "sinks" / "ops.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    uses = _handler_uses(tree)
    handlers = _approve_spec_handlers(tree)
    assert len(handlers) == 1
    assert [id(node) for node in uses] == [id(node) for node in handlers]


def test_the_handler_scan_finds_a_second_caller(tmp_path: Path) -> None:
    source = (
        "def approve(ctx, uow, data): ...\n"
        "def tick(ctx, uow): approve(ctx, uow, None)\n"
        "register(OperationSpec(name='finance.sink.approve', handler=approve))\n"
    )
    tree = ast.parse(source)
    assert len(_handler_uses(tree)) == 2
    assert len(_approve_spec_handlers(tree)) == 1
