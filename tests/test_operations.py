"""``dispatch``: refusals before any connection, the DSN the spec names, one
transaction.

Every test registers its own specs into an empty registry (the ``empty_registry``
fixture) and runs them against ``finance_test``.

The one exception to the rolled-back-transaction rule: ``dispatch`` commits for real, so
the tests that prove a commit (or its absence) write to a scratch table that
``probe_table`` creates committed. Its finalizer is registered before the table is
created and drops it whether the test passes, fails or errors, so nothing a test
commits outlives it.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import fields, replace
from typing import Any

import pytest
from pydantic import BaseModel, ConfigDict
from sqlalchemy import Connection, Engine, text
from sqlalchemy.engine import URL

from rheo_finance.context import ConnectionName, FinanceContext, UnitOfWork
from rheo_finance.contracts import Role, SafetyClass
from rheo_finance.operations import (
    HandlerEndedTransaction,
    InputInvalid,
    NoDatabases,
    OperationNotFound,
    OperationSpec,
    OutputInvalid,
    RoleRefused,
    dispatch,
    register,
)

MakeContext = Callable[[Role], FinanceContext]
ALL_ROLES = frozenset(Role)
OWNER_AND_SERVICE = frozenset({Role.OWNER, Role.SERVICE})


class Probe(BaseModel):
    model_config = ConfigDict(extra="forbid")
    label: str = "probe"
    count: int = 0


class Answer(BaseModel):
    value: str


class Unrelated(BaseModel):
    value: str = "x"


def make_spec(
    name: str,
    handler: Callable[..., Any],
    *,
    safety_class: SafetyClass = SafetyClass.READ,
    roles: frozenset[Role] = OWNER_AND_SERVICE,
    connection: ConnectionName = "owner",
) -> OperationSpec:
    return OperationSpec(
        name=name,
        input_model=Probe,
        output_model=Answer,
        handler=handler,
        safety_class=safety_class,
        roles=roles,
        connection=connection,
        description="A test operation.",
    )


def current_user(_ctx: FinanceContext, uow: UnitOfWork, _data: Probe) -> Answer:
    return Answer(value=str(uow.execute(text("SELECT current_user")).scalar()))


@pytest.fixture
def probe_table(request: pytest.FixtureRequest, owner_engine: Engine) -> str:
    """A committed scratch table in ``finance``, dropped however the test ends."""
    table = f"finance.dispatch_probe_{uuid.uuid4().hex[:8]}"

    def drop() -> None:
        with owner_engine.begin() as connection:
            connection.execute(text(f"DROP TABLE IF EXISTS {table}"))

    request.addfinalizer(drop)
    with owner_engine.begin() as connection:
        connection.execute(text(f"CREATE TABLE {table} (label text NOT NULL)"))
    return table


def probe_rows(engine: Engine, table: str) -> int:
    with engine.connect() as connection:
        return int(
            connection.execute(text(f"SELECT count(*) FROM {table}")).scalar_one()
        )


# --- refusals before any connection ---------------------------------------------------


def test_an_unknown_name_is_refused(
    empty_registry: dict[str, OperationSpec], make_context: MakeContext
) -> None:
    with pytest.raises(OperationNotFound):
        dispatch("finance.nothing.here", {}, Role.OWNER, ctx=make_context(Role.OWNER))


@pytest.mark.parametrize("role", [Role.MEMBER, Role.SERVICE])
def test_a_role_the_spec_does_not_list_is_refused_and_the_handler_never_runs(
    empty_registry: dict[str, OperationSpec], make_context: MakeContext, role: Role
) -> None:
    calls: list[Probe] = []

    def handler(_ctx: FinanceContext, _uow: UnitOfWork, data: Probe) -> Answer:
        calls.append(data)
        return Answer(value="ran")

    register(
        make_spec(
            "finance.probe.owner_only",
            handler,
            safety_class=SafetyClass.MUTATE,
            roles=frozenset({Role.OWNER}),
        )
    )
    with pytest.raises(RoleRefused, match=f"refuses role {role.value}"):
        dispatch("finance.probe.owner_only", {}, role, ctx=make_context(role))
    assert calls == []
    # The owner, which the spec lists, gets through.
    out = dispatch(
        "finance.probe.owner_only", {}, Role.OWNER, ctx=make_context(Role.OWNER)
    )
    assert out == Answer(value="ran")
    assert len(calls) == 1


def test_a_context_built_for_another_role_is_refused(
    empty_registry: dict[str, OperationSpec], make_context: MakeContext
) -> None:
    register(
        make_spec(
            "finance.probe.who", current_user, roles=ALL_ROLES, connection="reader"
        )
    )
    with pytest.raises(RoleRefused, match="context for member"):
        dispatch("finance.probe.who", {}, Role.OWNER, ctx=make_context(Role.MEMBER))


def test_input_is_validated_by_the_spec_model(
    empty_registry: dict[str, OperationSpec], make_context: MakeContext
) -> None:
    seen: list[Probe] = []

    def handler(_ctx: FinanceContext, _uow: UnitOfWork, data: Probe) -> Answer:
        seen.append(data)
        return Answer(value=data.label)

    register(make_spec("finance.probe.echo", handler))
    ctx = make_context(Role.OWNER)

    out = dispatch(
        "finance.probe.echo", {"label": "a", "count": "3"}, Role.OWNER, ctx=ctx
    )
    assert out == Answer(value="a")
    assert seen == [Probe(label="a", count=3)]

    out = dispatch("finance.probe.echo", Probe(label="b"), Role.OWNER, ctx=ctx)
    assert out == Answer(value="b")


def test_invalid_input_is_refused_without_echoing_the_value(
    empty_registry: dict[str, OperationSpec], make_context: MakeContext
) -> None:
    register(make_spec("finance.probe.echo", current_user))
    ctx = make_context(Role.OWNER)
    secret = "999-not-a-count"

    with pytest.raises(InputInvalid) as caught:
        dispatch("finance.probe.echo", {"count": secret}, Role.OWNER, ctx=ctx)
    assert "count" in str(caught.value)
    assert secret not in str(caught.value)
    assert caught.value.__cause__ is None and caught.value.__suppress_context__

    with pytest.raises(InputInvalid, match="extra"):
        dispatch("finance.probe.echo", {"other": 1}, Role.OWNER, ctx=ctx)
    with pytest.raises(InputInvalid, match="takes Probe, not Unrelated"):
        dispatch("finance.probe.echo", Unrelated(), Role.OWNER, ctx=ctx)


# --- the DSN the spec names -----------------------------------------------------------


@pytest.mark.parametrize(
    ("connection", "expected"),
    [("owner", "finance_owner"), ("reader", "finance_reader")],
)
def test_dispatch_opens_the_dsn_the_spec_names(
    empty_registry: dict[str, OperationSpec],
    make_context: MakeContext,
    connection: ConnectionName,
    expected: str,
) -> None:
    roles = ALL_ROLES if connection == "reader" else OWNER_AND_SERVICE
    register(
        make_spec("finance.probe.who", current_user, roles=roles, connection=connection)
    )
    out = dispatch("finance.probe.who", {}, Role.OWNER, ctx=make_context(Role.OWNER))
    assert out == Answer(value=expected)


def test_a_member_call_runs_as_the_reader(
    empty_registry: dict[str, OperationSpec], make_context: MakeContext
) -> None:
    register(
        make_spec(
            "finance.probe.who", current_user, roles=ALL_ROLES, connection="reader"
        )
    )
    out = dispatch("finance.probe.who", {}, Role.MEMBER, ctx=make_context(Role.MEMBER))
    assert out == Answer(value="finance_reader")


# --- one transaction ------------------------------------------------------------------


def test_a_handler_that_returns_commits(
    empty_registry: dict[str, OperationSpec],
    make_context: MakeContext,
    owner_engine: Engine,
    probe_table: str,
) -> None:
    def insert(_ctx: FinanceContext, uow: UnitOfWork, data: Probe) -> Answer:
        uow.execute(
            text(f"INSERT INTO {probe_table} (label) VALUES (:label)"),
            {"label": data.label},
        )
        return Answer(value="inserted")

    register(make_spec("finance.probe.insert", insert, safety_class=SafetyClass.MUTATE))
    dispatch("finance.probe.insert", {}, Role.OWNER, ctx=make_context(Role.OWNER))
    assert probe_rows(owner_engine, probe_table) == 1


class HandlerBroke(Exception):
    pass


def test_a_handler_error_rolls_back_and_is_reraised(
    empty_registry: dict[str, OperationSpec],
    make_context: MakeContext,
    owner_engine: Engine,
    probe_table: str,
) -> None:
    def insert_then_fail(_ctx: FinanceContext, uow: UnitOfWork, _data: Probe) -> Answer:
        uow.execute(text(f"INSERT INTO {probe_table} (label) VALUES ('a')"))
        uow.execute(text(f"INSERT INTO {probe_table} (label) VALUES ('b')"))
        raise HandlerBroke("after two writes")

    register(
        make_spec(
            "finance.probe.fail", insert_then_fail, safety_class=SafetyClass.MUTATE
        )
    )
    with pytest.raises(HandlerBroke):
        dispatch("finance.probe.fail", {}, Role.OWNER, ctx=make_context(Role.OWNER))
    assert probe_rows(owner_engine, probe_table) == 0


def test_a_wrong_output_type_rolls_back(
    empty_registry: dict[str, OperationSpec],
    make_context: MakeContext,
    owner_engine: Engine,
    probe_table: str,
) -> None:
    def insert_wrong(_ctx: FinanceContext, uow: UnitOfWork, _data: Probe) -> Unrelated:
        uow.execute(text(f"INSERT INTO {probe_table} (label) VALUES ('a')"))
        return Unrelated()

    register(
        make_spec("finance.probe.wrong", insert_wrong, safety_class=SafetyClass.MUTATE)
    )
    with pytest.raises(OutputInvalid, match="returned Unrelated, not Answer"):
        dispatch("finance.probe.wrong", {}, Role.OWNER, ctx=make_context(Role.OWNER))
    assert probe_rows(owner_engine, probe_table) == 0


def test_a_handler_cannot_commit_through_its_unit_of_work(
    empty_registry: dict[str, OperationSpec],
    make_context: MakeContext,
    owner_engine: Engine,
    probe_table: str,
) -> None:
    def write_and_commit(_ctx: FinanceContext, uow: UnitOfWork, _data: Probe) -> Answer:
        uow.execute(text(f"INSERT INTO {probe_table} (label) VALUES ('a')"))
        uow.commit()  # type: ignore[attr-defined]  # the attempt under test
        return Answer(value="committed")

    register(
        make_spec(
            "finance.probe.commits", write_and_commit, safety_class=SafetyClass.MUTATE
        )
    )
    with pytest.raises(AttributeError):
        dispatch("finance.probe.commits", {}, Role.OWNER, ctx=make_context(Role.OWNER))
    assert probe_rows(owner_engine, probe_table) == 0


def test_a_unit_of_work_offers_three_methods_of_its_own(
    owner_conn: Connection,
) -> None:
    uow = UnitOfWork(owner_conn)
    public = sorted(name for name in dir(uow) if not name.startswith("_"))
    assert public == ["execute", "scalar", "scalars"]
    for name in public:
        # Bound to the unit of work, not to the raw connection behind it.
        assert getattr(uow, name).__self__ is uow, name
    assert uow.scalar(text("SELECT 1")) == 1


def test_a_commit_statement_from_a_handler_is_detected(
    empty_registry: dict[str, OperationSpec],
    make_context: MakeContext,
    probe_table: str,
) -> None:
    # A COMMIT sent as SQL cannot be stopped client-side; dispatch reports it.
    def sql_commit(_ctx: FinanceContext, uow: UnitOfWork, _data: Probe) -> Answer:
        uow.execute(text(f"INSERT INTO {probe_table} (label) VALUES ('a')"))
        uow.execute(text("COMMIT"))
        return Answer(value="committed")

    register(
        make_spec(
            "finance.probe.sql_commit", sql_commit, safety_class=SafetyClass.MUTATE
        )
    )
    with pytest.raises(HandlerEndedTransaction):
        dispatch(
            "finance.probe.sql_commit", {}, Role.OWNER, ctx=make_context(Role.OWNER)
        )


# --- the DSNs stay out of a handler's reach -----------------------------------------


def test_a_context_without_databases_is_refused(
    empty_registry: dict[str, OperationSpec], make_context: MakeContext
) -> None:
    register(make_spec("finance.probe.who", current_user))
    bare = replace(make_context(Role.OWNER), _databases=None)
    with pytest.raises(NoDatabases):
        dispatch("finance.probe.who", {}, Role.OWNER, ctx=bare)


def test_a_handler_context_carries_no_dsn_or_engine(
    empty_registry: dict[str, OperationSpec], make_context: MakeContext
) -> None:
    def inspect(ctx: FinanceContext, _uow: UnitOfWork, _data: Probe) -> Answer:
        for item in fields(ctx):
            value = getattr(ctx, item.name)
            assert not isinstance(value, (Engine, URL, Connection)), item.name
            assert type(value).__name__ != "_Databases", item.name
        return Answer(value="clean")

    register(make_spec("finance.probe.inspect", inspect))
    out = dispatch(
        "finance.probe.inspect", {}, Role.OWNER, ctx=make_context(Role.OWNER)
    )
    assert out == Answer(value="clean")


def test_a_handler_cannot_dispatch_again(
    empty_registry: dict[str, OperationSpec],
    make_context: MakeContext,
    owner_engine: Engine,
    probe_table: str,
) -> None:
    # A nested dispatch would open a second connection outside this transaction.
    def nested(ctx: FinanceContext, uow: UnitOfWork, _data: Probe) -> Answer:
        uow.execute(text(f"INSERT INTO {probe_table} (label) VALUES ('a')"))
        return dispatch("finance.probe.inner", {}, Role.OWNER, ctx=ctx)  # type: ignore[return-value]

    def inner(_ctx: FinanceContext, uow: UnitOfWork, _data: Probe) -> Answer:
        uow.execute(text(f"INSERT INTO {probe_table} (label) VALUES ('b')"))
        return Answer(value="inner")

    mutate = SafetyClass.MUTATE
    register(make_spec("finance.probe.outer", nested, safety_class=mutate))
    register(make_spec("finance.probe.inner", inner, safety_class=mutate))
    with pytest.raises(NoDatabases):
        dispatch("finance.probe.outer", {}, Role.OWNER, ctx=make_context(Role.OWNER))
    assert probe_rows(owner_engine, probe_table) == 0
