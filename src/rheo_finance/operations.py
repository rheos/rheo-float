"""The operation registry and ``dispatch`` (FR-52, FR-48, Key decision 21).

Every capability is one ``OperationSpec`` in ``SPECS``, named ``finance.<noun>.<verb>``.
The CLI, jobs and report commands reach the database only through ``dispatch``.

Each spec names its connection. The rules, checked when a spec is built:

- a spec that lists role ``member`` uses the ``reader`` connection;
- a MUTATE or DRAFT spec uses the ``owner`` connection;
- a ``tool_name`` appears only on a READ spec on the ``reader`` connection;
- an ``owner_only`` spec lists role ``owner`` and no other.

A READ spec declares ``reader`` only when its handler selects from ``report_*`` views
alone. Nothing here can check that from the outside: the reader role's grants do, since
it can select from the report views and nothing else, so a ``reader`` spec that touches
a base table fails under the reader DSN in its own test.

Outbound pushes (M.O.T., Telegram) are sink adapters, never registered operations.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import replace
from typing import Any, Self

import psycopg
from psycopg import pq
from pydantic import BaseModel, ConfigDict, ValidationError, model_validator
from sqlalchemy import Connection, Engine, Transaction, create_engine, text
from sqlalchemy.engine import URL
from sqlalchemy.pool import NullPool

from rheo_finance.context import (
    ConnectionName,
    FinanceContext,
    UnitOfWork,
)
from rheo_finance.contracts import Role, SafetyClass

NAME_SHAPE = re.compile(r"finance\.[a-z_]+\.[a-z_]+")
TOOL_NAME_SHAPE = re.compile(r"finance_[a-z_]+")


class OperationError(Exception):
    """A refusal by the registry or by ``dispatch``. Messages carry no input values."""


class OperationNotFound(OperationError, LookupError):
    pass


class DuplicateOperation(OperationError):
    pass


class RoleRefused(OperationError):
    pass


class InputInvalid(OperationError):
    pass


class OutputInvalid(OperationError):
    pass


class HandlerEndedTransaction(OperationError):
    pass


class NoDatabases(OperationError):
    pass


class _Databases:
    """The owner and reader DSNs, one engine per DSN, created on first use.

    Only this module touches it: a guard test fails on any other module under
    ``src/rheo_finance`` that names it, ``_databases`` or ``with_databases``.
    """

    def __init__(
        self,
        owner_url: URL,
        reader_url: URL,
        owner_connection: Connection | None = None,
    ) -> None:
        self._urls: dict[ConnectionName, URL] = {
            "owner": owner_url,
            "reader": reader_url,
        }
        self._engines: dict[ConnectionName, Engine] = {}
        self.owner_connection = owner_connection

    def engine(self, name: ConnectionName) -> Engine:
        engine = self._engines.get(name)
        if engine is None:
            # NullPool: a CLI call or a job tick opens few connections, and a pooled
            # connection would outlive the role and grant state it was opened under.
            engine = create_engine(self._urls[name], poolclass=NullPool)
            self._engines[name] = engine
        return engine


def with_databases(
    ctx: FinanceContext, *, owner_url: URL, reader_url: URL
) -> FinanceContext:
    """``ctx`` carrying the owner and reader DSNs that ``dispatch`` opens.

    For the composition roots (the CLI, jobs, tests) only. A handler receives a context
    without them.
    """
    return replace(ctx, _databases=_Databases(owner_url, reader_url))


def with_owner_connection(
    ctx: FinanceContext, connection: Connection
) -> FinanceContext:
    """``ctx`` with every owner-connection operation run on ``connection``.

    For tests only (the DSN-holder guard keeps it out of ``src``): ``dispatch`` runs
    the handler inside a savepoint on ``connection`` and "commits" by releasing it, so
    the caller's own transaction decides what survives.
    Reader operations still open the reader DSN. ``ctx`` must already carry databases
    (``with_databases``).
    """
    databases = ctx._databases
    if databases is None:
        raise NoDatabases("with_owner_connection needs a context with databases")
    return replace(
        ctx,
        _databases=_Databases(
            databases._urls["owner"], databases._urls["reader"], connection
        ),
    )


@contextmanager
def _opened(
    databases: _Databases, name: ConnectionName
) -> Iterator[tuple[Connection, Transaction]]:
    """The connection and transaction an operation runs in."""
    injected = databases.owner_connection if name == "owner" else None
    if injected is not None:
        yield injected, injected.begin_nested()
        return
    with databases.engine(name).connect() as connection:
        yield connection, connection.begin()


class OperationSpec(BaseModel):
    """One registered operation."""

    model_config = ConfigDict(frozen=True, extra="forbid", arbitrary_types_allowed=True)

    name: str
    input_model: type[BaseModel]
    output_model: type[BaseModel]
    # Callable[..., ...]: each handler takes its own input model, and Callable
    # parameters are contravariant, so no fixed parameter list admits them all.
    # dispatch passes it only an instance of input_model and checks the result.
    handler: Callable[..., BaseModel]
    safety_class: SafetyClass
    roles: frozenset[Role]
    connection: ConnectionName
    tool_name: str | None = None
    description: str
    # Only the owner may ever run it (the sink approval; the bulk confirm from prompt
    # 77): an approval a job (role service) could reach would let a tick turn a sink
    # on. The flag makes widening its roles a refused spec, not a quiet edit.
    owner_only: bool = False

    @model_validator(mode="after")
    def _check(self) -> Self:
        if not NAME_SHAPE.fullmatch(self.name):
            raise ValueError(f"{self.name!r} is not finance.<noun>.<verb>")
        if not self.roles:
            raise ValueError(f"{self.name}: lists no role")
        if not self.description.strip():
            raise ValueError(f"{self.name}: has no description")
        if self.owner_only and self.roles != {Role.OWNER}:
            raise ValueError(f"{self.name}: lists a role other than owner")
        if Role.MEMBER in self.roles and self.connection != "reader":
            raise ValueError(f"{self.name}: lists role member, so it must use reader")
        if self.safety_class is not SafetyClass.READ and self.connection != "owner":
            raise ValueError(
                f"{self.name}: a {self.safety_class.value} operation must use owner"
            )
        if self.tool_name is not None:
            if self.safety_class is not SafetyClass.READ or self.connection != "reader":
                raise ValueError(
                    f"{self.name}: only a READ operation on reader may name a tool"
                )
            if not TOOL_NAME_SHAPE.fullmatch(self.tool_name):
                raise ValueError(
                    f"{self.name}: {self.tool_name!r} is not finance_<verb>"
                )
        return self


SPECS: dict[str, OperationSpec] = {}


def register(spec: OperationSpec) -> None:
    """Add ``spec`` to ``SPECS``. A name registered twice is refused."""
    if not isinstance(spec, OperationSpec):
        raise TypeError(f"register takes an OperationSpec, not {type(spec).__name__}")
    if spec.name in SPECS:
        raise DuplicateOperation(f"{spec.name} is already registered")
    SPECS[spec.name] = spec


def _server_in_transaction(connection: Connection) -> bool:
    """Whether the server still holds this connection's transaction open.

    A handler's SQL ``COMMIT`` or ``ROLLBACK`` ends it on the server while SQLAlchemy
    still counts it active, and anything the handler wrote before it has landed.
    """
    driver = connection.connection.driver_connection
    if not isinstance(driver, psycopg.Connection):
        raise TypeError("dispatch expects a psycopg 3 connection")
    return driver.info.transaction_status is not pq.TransactionStatus.IDLE


def _location(loc: tuple[int | str, ...]) -> str:
    return ".".join(str(part) for part in loc) or "<input>"


def _validated(spec: OperationSpec, payload: BaseModel | dict[str, Any]) -> BaseModel:
    if isinstance(payload, BaseModel) and not isinstance(payload, spec.input_model):
        raise InputInvalid(
            f"{spec.name} takes {spec.input_model.__name__},"
            f" not {type(payload).__name__}"
        )
    try:
        if isinstance(payload, BaseModel):
            return spec.input_model.model_validate(payload.model_dump())
        return spec.input_model.model_validate(payload)
    except ValidationError as exc:
        # Name the fields and the error kinds only: a ValidationError's own text
        # repeats the input, which can be restricted data.
        problems = "; ".join(
            f"{_location(error['loc'])}: {error['type']}" for error in exc.errors()
        )
        raise InputInvalid(f"{spec.name}: invalid input ({problems})") from None


def dispatch(
    name: str,
    payload: BaseModel | dict[str, Any],
    role: Role,
    *,
    ctx: FinanceContext,
) -> BaseModel:
    """Run operation ``name`` as ``role`` and return its output.

    Refuses an unknown name, a role the spec does not list, a context built for another
    role, an input its model rejects and a context with no databases (a handler's own
    context is one), all before any connection is opened. Then it
    opens the DSN the spec's ``connection`` names and runs the handler in one
    transaction: committed when the handler returns its output model, rolled back on any
    error, which is re-raised.
    """
    spec = SPECS.get(name)
    if spec is None:
        raise OperationNotFound(f"no operation named {name!r}")
    if role not in spec.roles:
        raise RoleRefused(f"{name} refuses role {role.value}")
    if ctx.role is not role:
        raise RoleRefused(
            f"{name}: called as {role.value} with a context for {ctx.role.value}"
        )
    data = _validated(spec, payload)
    databases = ctx._databases
    if databases is None:
        # Also what a handler gets if it tries to dispatch from inside its operation.
        raise NoDatabases(f"{name}: the context carries no databases")
    handler_ctx = replace(ctx, _databases=None)

    with _opened(databases, spec.connection) as (connection, transaction):
        try:
            # psycopg sends BEGIN with the first statement: open the server transaction
            # now, so an idle server after the handler means the handler ended it.
            connection.execute(text("SELECT 1"))
            result = spec.handler(handler_ctx, UnitOfWork(connection), data)
            if not transaction.is_active or not _server_in_transaction(connection):
                raise HandlerEndedTransaction(
                    f"{name}: the handler ended its transaction"
                )
            if not isinstance(result, spec.output_model):
                raise OutputInvalid(
                    f"{name} returned {type(result).__name__},"
                    f" not {spec.output_model.__name__}"
                )
            transaction.commit()
        except BaseException:
            if transaction.is_active:
                transaction.rollback()
            raise
    return result
