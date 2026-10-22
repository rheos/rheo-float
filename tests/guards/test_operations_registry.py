"""AC-53: the operation registry, the CLI's import boundary, and no push as an
operation.

- Every registered operation is named ``finance.<noun>.<verb>``, takes and returns
  pydantic models, is classed READ, DRAFT or MUTATE, and names its connection, owner or
  reader. (The connection rules of Key decision 21 are in
  ``tests/test_operations_invariants.py``.)
- No module in ``src/rheo_finance/cli`` imports sqlalchemy or ``rheo_finance.storage``,
  at any depth (a function-local import counts), so the CLI reaches the database only
  through ``dispatch``.
- The M.O.T. and Telegram pushes are sink adapters, never operations: no operation's
  noun is ``mot``, ``telegram`` or a push, and no handler lives in a sink adapter module
  or on a sink adapter class.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from pathlib import Path

import pytest
from pydantic import BaseModel

from rheo_finance import catalogue
from rheo_finance.contracts import Role, SafetyClass
from rheo_finance.operations import OperationSpec

from .source_tree import PACKAGE, copy_package, imports, modules, names_module, plant

NAME = re.compile(r"finance\.[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*")
CLASSES = {SafetyClass.READ, SafetyClass.DRAFT, SafetyClass.MUTATE}
CONNECTIONS = {"owner", "reader"}
PUSH_NOUNS = {"mot", "telegram", "push", "notify", "notification"}
# A sink adapter module is a module under rheo_finance.sinks named for its sink.
ADAPTER_MODULE = re.compile(r"rheo_finance\.sinks\.(mot|telegram)(\..+)?")


def registry_problems(specs: Iterable[OperationSpec]) -> list[str]:
    problems: list[str] = []
    for spec in specs:
        name = spec.name
        if not NAME.fullmatch(name):
            problems.append(f"{name}: not finance.<noun>.<verb>")
        for what, model in (("input", spec.input_model), ("output", spec.output_model)):
            if not (isinstance(model, type) and issubclass(model, BaseModel)):
                problems.append(f"{name}: the {what} model is not a pydantic model")
        if spec.safety_class not in CLASSES:
            problems.append(
                f"{name}: class {spec.safety_class} is not READ/DRAFT/MUTATE"
            )
        if spec.connection not in CONNECTIONS:
            problems.append(f"{name}: names no connection from owner, reader")
        noun = name.split(".")[1] if name.count(".") == 2 else ""
        if noun in PUSH_NOUNS:
            problems.append(f"{name}: a push is a sink adapter, not an operation")
        handler_module = getattr(spec.handler, "__module__", "") or ""
        qualname = getattr(spec.handler, "__qualname__", "") or ""
        if ADAPTER_MODULE.fullmatch(handler_module) or qualname.split(".")[0].endswith(
            "Sink"
        ):
            problems.append(f"{name}: the handler is a sink adapter's")
    return problems


def test_every_registered_operation_is_well_formed() -> None:
    specs = catalogue.load_all()
    assert specs, "the control: the catalogue has operations to check"
    assert registry_problems(specs.values()) == []


class _In(BaseModel):
    pass


class _Out(BaseModel):
    pass


def _handler(_ctx: object, _uow: object, _data: _In) -> _Out:
    return _Out()


class MotSink:
    def push(self, _ctx: object, _uow: object, _data: _In) -> _Out:
        return _Out()


def adapter_handler(_ctx: object, _uow: object, _data: _In) -> _Out:
    return _Out()


adapter_handler.__module__ = "rheo_finance.sinks.mot"


def planted(**changes: object) -> OperationSpec:
    """A spec built without its validator, as a weakened one would let through."""
    fields: dict[str, object] = {
        "name": "finance.fixture.view",
        "input_model": _In,
        "output_model": _Out,
        "handler": _handler,
        "safety_class": SafetyClass.READ,
        "roles": frozenset({Role.OWNER}),
        "connection": "owner",
        "tool_name": None,
        "description": "A fixture operation.",
        "owner_only": False,
    }
    fields.update(changes)
    return OperationSpec.model_construct(**fields)  # type: ignore[arg-type]


def test_a_well_formed_fixture_passes() -> None:
    assert registry_problems([planted()]) == []


@pytest.mark.parametrize(
    ("changes", "problem"),
    [
        ({"name": "finance.view"}, "not finance.<noun>.<verb>"),
        ({"name": "ledger.fixture.view"}, "not finance.<noun>.<verb>"),
        ({"input_model": dict}, "the input model is not a pydantic model"),
        ({"output_model": int}, "the output model is not a pydantic model"),
        ({"safety_class": "financial"}, "is not READ/DRAFT/MUTATE"),
        ({"connection": "superuser"}, "names no connection"),
        ({"connection": None}, "names no connection"),
        ({"name": "finance.mot.push"}, "a push is a sink adapter"),
        ({"name": "finance.telegram.send"}, "a push is a sink adapter"),
        ({"handler": MotSink().push}, "the handler is a sink adapter's"),
        ({"handler": adapter_handler}, "the handler is a sink adapter's"),
    ],
)
def test_a_planted_bad_spec_is_found(changes: dict[str, object], problem: str) -> None:
    problems = registry_problems([planted(**changes)])
    assert any(problem in each for each in problems), problems


# --- the CLI's import boundary --------------------------------------------------------

CLI = "rheo_finance.cli"
FORBIDDEN = ("sqlalchemy", "rheo_finance.storage")


def cli_boundary_problems(root: Path = PACKAGE) -> list[str]:
    return sorted(
        f"{module.name}:{each.line} imports {each.name}"
        for module in modules(root)
        if names_module(module.name, CLI)
        for each in imports(module)
        if any(names_module(each.name, target) for target in FORBIDDEN)
    )


def test_the_cli_imports_neither_sqlalchemy_nor_storage() -> None:
    assert cli_boundary_problems() == []
    # The control: the scan reads the CLI package.
    assert any(names_module(module.name, CLI) for module in modules())


@pytest.mark.parametrize(
    "source",
    [
        "import sqlalchemy\n",
        "from sqlalchemy import select\n",
        "def run():\n    from sqlalchemy.engine import URL\n",
        "from rheo_finance.storage import schema\n",
        "from rheo_finance import storage\n",
        "from ..storage.schema import decision\n",
        "import rheo_finance.storage.ids\n",
    ],
)
def test_a_planted_cli_import_is_found(source: str, tmp_path: Path) -> None:
    root = copy_package(tmp_path)
    plant(root, "cli/planted.py", source)
    problems = cli_boundary_problems(root)
    assert problems and all(
        each.startswith("rheo_finance.cli.planted:") for each in problems
    )
