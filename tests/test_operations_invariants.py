"""The connection invariants of Key decision 21, over whatever is registered.

Each invariant runs over three registries: the installed ``SPECS`` as the build ships
it, an empty one, and a fixture one that covers every class and connection. The rules
are restated here rather than imported, so a weakened check in ``operations`` is caught
by a spec that reaches ``SPECS`` anyway.
"""

from __future__ import annotations

import ast
import re
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from psycopg import errors as pg_errors
from pydantic import BaseModel, ValidationError
from sqlalchemy import text
from sqlalchemy.exc import ProgrammingError

from rheo_finance import catalogue, operations
from rheo_finance.context import ConnectionName, FinanceContext, UnitOfWork
from rheo_finance.contracts import Role, SafetyClass
from rheo_finance.operations import (
    DuplicateOperation,
    OperationSpec,
    dispatch,
    register,
)

MakeContext = Callable[[Role], FinanceContext]
ALLOWED_CLASSES = {SafetyClass.READ, SafetyClass.DRAFT, SafetyClass.MUTATE}
NAME = re.compile(r"^finance\.[a-z_]+\.[a-z_]+$")
EVERYONE = frozenset(Role)
OWNER_AND_SERVICE = frozenset({Role.OWNER, Role.SERVICE})
OWNER_ONLY = frozenset({Role.OWNER})


class Empty(BaseModel):
    pass


class Rows(BaseModel):
    count: int


def no_op(_ctx: FinanceContext, _uow: UnitOfWork, _data: Empty) -> Rows:
    return Rows(count=0)


def make_spec(
    name: str,
    *,
    safety_class: SafetyClass,
    roles: frozenset[Role],
    connection: ConnectionName,
    tool_name: str | None = None,
    handler: Callable[..., Any] = no_op,
) -> OperationSpec:
    return OperationSpec(
        name=name,
        input_model=Empty,
        output_model=Rows,
        handler=handler,
        safety_class=safety_class,
        roles=roles,
        connection=connection,
        tool_name=tool_name,
        description="A fixture operation.",
    )


def fixture_specs() -> list[OperationSpec]:
    """One valid spec per shape the catalogue uses."""
    read, draft, mutate = SafetyClass.READ, SafetyClass.DRAFT, SafetyClass.MUTATE
    return [
        make_spec(
            "finance.fixture.view_read",
            safety_class=read,
            roles=EVERYONE,
            connection="reader",
            tool_name="finance_fixture",
        ),
        make_spec(
            "finance.fixture.view_untooled",
            safety_class=read,
            roles=EVERYONE,
            connection="reader",
        ),
        make_spec(
            "finance.fixture.base_read",
            safety_class=read,
            roles=OWNER_AND_SERVICE,
            connection="owner",
        ),
        make_spec(
            "finance.fixture.change",
            safety_class=mutate,
            roles=OWNER_AND_SERVICE,
            connection="owner",
        ),
        make_spec(
            "finance.fixture.approve",
            safety_class=mutate,
            roles=OWNER_ONLY,
            connection="owner",
        ),
        make_spec(
            "finance.fixture.draft",
            safety_class=draft,
            roles=OWNER_ONLY,
            connection="owner",
        ),
    ]


@pytest.fixture(params=["installed", "empty", "fixture"])
def registry(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> Iterator[list[OperationSpec]]:
    if request.param == "installed":
        yield list(catalogue.load_all().values())
        return
    monkeypatch.setattr(operations, "SPECS", {})
    if request.param == "fixture":
        for spec in fixture_specs():
            register(spec)
        assert len(operations.SPECS) == len(fixture_specs())
    yield list(operations.SPECS.values())


# --- the invariants, over each registry -----------------------------------------------


def test_every_member_spec_uses_the_reader(registry: list[OperationSpec]) -> None:
    for spec in registry:
        if Role.MEMBER in spec.roles:
            assert spec.connection == "reader", spec.name


def test_every_mutate_and_draft_spec_uses_the_owner(
    registry: list[OperationSpec],
) -> None:
    for spec in registry:
        if spec.safety_class in (SafetyClass.MUTATE, SafetyClass.DRAFT):
            assert spec.connection == "owner", spec.name


def test_tool_names_only_on_reader_read_specs(registry: list[OperationSpec]) -> None:
    for spec in registry:
        if spec.tool_name is not None:
            assert spec.safety_class is SafetyClass.READ, spec.name
            assert spec.connection == "reader", spec.name


def test_every_class_is_read_draft_or_mutate(registry: list[OperationSpec]) -> None:
    for spec in registry:
        assert spec.safety_class in ALLOWED_CLASSES, spec.name


def test_every_name_is_finance_noun_verb(registry: list[OperationSpec]) -> None:
    for spec in registry:
        assert NAME.match(spec.name), spec.name
        assert operations.SPECS[spec.name] is spec


# --- a spec that breaks a rule never reaches the registry -----------------------------


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        (
            {
                "safety_class": SafetyClass.READ,
                "roles": EVERYONE,
                "connection": "owner",
            },
            "lists role member",
        ),
        (
            {
                "safety_class": SafetyClass.MUTATE,
                "roles": OWNER_ONLY,
                "connection": "reader",
            },
            "mutate operation must use owner",
        ),
        (
            {
                "safety_class": SafetyClass.DRAFT,
                "roles": OWNER_ONLY,
                "connection": "reader",
            },
            "draft operation must use owner",
        ),
        (
            {
                "safety_class": SafetyClass.READ,
                "roles": OWNER_AND_SERVICE,
                "connection": "owner",
                "tool_name": "finance_base",
            },
            "only a READ operation on reader may name a tool",
        ),
        (
            {
                "safety_class": SafetyClass.MUTATE,
                "roles": OWNER_ONLY,
                "connection": "owner",
                "tool_name": "finance_change",
            },
            "only a READ operation on reader may name a tool",
        ),
        (
            {
                "safety_class": SafetyClass.READ,
                "roles": EVERYONE,
                "connection": "reader",
                "tool_name": "status",
            },
            "is not finance_<verb>",
        ),
        (
            {
                "safety_class": SafetyClass.READ,
                "roles": frozenset(),
                "connection": "reader",
            },
            "lists no role",
        ),
    ],
)
def test_a_spec_that_breaks_a_rule_is_refused(
    kwargs: dict[str, Any], message: str
) -> None:
    with pytest.raises(ValidationError, match=message):
        make_spec("finance.fixture.bad", **kwargs)


@pytest.mark.parametrize(
    "name", ["fixture.bad", "finance.bad", "finance.Bad.get", "finance.a.b.c", "x.a.b"]
)
def test_a_name_off_the_pattern_is_refused(name: str) -> None:
    with pytest.raises(ValidationError, match="finance.<noun>.<verb>"):
        make_spec(
            name, safety_class=SafetyClass.READ, roles=OWNER_ONLY, connection="owner"
        )


@pytest.mark.parametrize("safety_class", ["financial", "external", "destructive"])
def test_a_class_beyond_the_three_is_refused(safety_class: str) -> None:
    with pytest.raises(ValidationError):
        OperationSpec.model_validate(
            {
                "name": "finance.fixture.bad",
                "input_model": Empty,
                "output_model": Rows,
                "handler": no_op,
                "safety_class": safety_class,
                "roles": OWNER_ONLY,
                "connection": "owner",
                "description": "A fixture operation.",
            }
        )


def test_a_name_registers_once(empty_registry: dict[str, OperationSpec]) -> None:
    spec = make_spec(
        "finance.fixture.once",
        safety_class=SafetyClass.READ,
        roles=OWNER_ONLY,
        connection="owner",
    )
    register(spec)
    with pytest.raises(DuplicateOperation):
        register(spec)


# --- the reader's grants enforce the rule from the first operation --------------------


def count_versions(_ctx: FinanceContext, uow: UnitOfWork, _data: Empty) -> Rows:
    # A base table: the reader may select from report views and nothing else.
    query = text("SELECT count(*) FROM finance.alembic_version_finance")
    return Rows(count=int(uow.execute(query).scalar_one()))


def test_a_reader_spec_that_reads_a_base_table_fails_with_a_permission_error(
    empty_registry: dict[str, OperationSpec], make_context: MakeContext
) -> None:
    register(
        make_spec(
            "finance.fixture.sneaky_read",
            safety_class=SafetyClass.READ,
            roles=EVERYONE,
            connection="reader",
            handler=count_versions,
        )
    )
    for role in (Role.MEMBER, Role.OWNER):
        with pytest.raises(ProgrammingError) as caught:
            dispatch("finance.fixture.sneaky_read", {}, role, ctx=make_context(role))
        assert isinstance(caught.value.orig, pg_errors.InsufficientPrivilege)


def test_the_same_read_on_the_owner_connection_succeeds(
    empty_registry: dict[str, OperationSpec], make_context: MakeContext
) -> None:
    # The control for the test above: the query is sound, only the role differs.
    register(
        make_spec(
            "finance.fixture.base_read",
            safety_class=SafetyClass.READ,
            roles=OWNER_AND_SERVICE,
            connection="owner",
            handler=count_versions,
        )
    )
    out = dispatch(
        "finance.fixture.base_read", {}, Role.OWNER, ctx=make_context(Role.OWNER)
    )
    assert out == Rows(count=1)


# --- the catalogue and the DSN boundary, over the source tree -----------------------

SRC = Path(operations.__file__).resolve().parent


def _modules() -> Iterator[tuple[str, ast.Module]]:
    for path in sorted(SRC.rglob("*.py")):
        relative = path.relative_to(SRC.parent).with_suffix("")
        dotted = ".".join(relative.parts).removesuffix(".__init__")
        yield dotted, ast.parse(path.read_text(encoding="utf-8"))


def _names(tree: ast.Module) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            found.add(node.id)
        elif isinstance(node, ast.Attribute):
            found.add(node.attr)
        elif isinstance(node, ast.alias):
            found.add(node.name.rsplit(".", 1)[-1])
        elif isinstance(node, ast.keyword) and node.arg:
            found.add(node.arg)
    return found


# Who may name the DSN holder: operations (it owns it) and context (it declares the
# field dispatch reads). A composition root that builds contexts (the CLI, jobs) joins
# this list in the change that adds it.
HOLDER_NAMES = {"_Databases", "_databases", "with_databases"}
HOLDER_ALLOWED = {"rheo_finance.operations", "rheo_finance.context"}
# Who may make an engine: dispatch, and the migration path.
ENGINE_ALLOWED = {
    "rheo_finance.operations",
    "rheo_finance.db",
    "rheo_finance.migrations.env",
}


def test_only_operations_reaches_the_dsn_holder() -> None:
    offenders = sorted(
        f"{module}: {', '.join(sorted(_names(tree) & HOLDER_NAMES))}"
        for module, tree in _modules()
        if module not in HOLDER_ALLOWED and _names(tree) & HOLDER_NAMES
    )
    assert offenders == []


def test_only_dispatch_and_migrations_create_engines() -> None:
    offenders = sorted(
        module
        for module, tree in _modules()
        if module not in ENGINE_ALLOWED and "create_engine" in _names(tree)
    )
    assert offenders == []


def test_every_module_that_registers_is_in_the_catalogue() -> None:
    registering = {
        module
        for module, tree in _modules()
        if module != "rheo_finance.operations"
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and (
            (isinstance(node.func, ast.Name) and node.func.id == "register")
            or (isinstance(node.func, ast.Attribute) and node.func.attr == "register")
        )
    }
    assert registering <= set(catalogue.MODULES)
    assert len(set(catalogue.MODULES)) == len(catalogue.MODULES)


def test_a_handler_that_is_not_callable_is_refused() -> None:
    with pytest.raises(ValidationError, match="callable"):
        make_spec(
            "finance.fixture.bad",
            safety_class=SafetyClass.READ,
            roles=OWNER_ONLY,
            connection="owner",
            handler=42,  # type: ignore[arg-type]
        )
