"""The operation registry and ``dispatch`` (FR-52, FR-48, Key decision 21).

Every capability is one ``OperationSpec`` in ``SPECS``, named ``finance.<noun>.<verb>``.
The CLI, jobs and report commands reach the database only through ``dispatch``.

Each spec names its connection. The rules, checked when a spec is built:

- a spec that lists role ``member`` uses the ``reader`` connection;
- a MUTATE or DRAFT spec uses the ``owner`` connection;
- a ``tool_name`` appears only on a READ spec on the ``reader`` connection.

A READ spec declares ``reader`` only when its handler selects from ``report_*`` views
alone. Nothing here can check that from the outside: the reader role's grants do, since
it can select from the report views and nothing else, so a ``reader`` spec that touches
a base table fails under the reader DSN in its own test.

Outbound pushes (M.O.T., Telegram) are sink adapters, never registered operations.
"""

from __future__ import annotations

import re
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, ValidationError, model_validator

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


class OperationSpec(BaseModel):
    """One registered operation."""

    model_config = ConfigDict(frozen=True, extra="forbid", arbitrary_types_allowed=True)

    name: str
    input_model: type[BaseModel]
    output_model: type[BaseModel]
    # Any: each handler takes its own input model, and Callable parameters are
    # contravariant, so no narrower type admits them all. dispatch passes it only an
    # instance of input_model and checks the result against output_model.
    handler: Any
    safety_class: SafetyClass
    roles: frozenset[Role]
    connection: ConnectionName
    tool_name: str | None = None
    description: str

    @model_validator(mode="after")
    def _check(self) -> Self:
        if not NAME_SHAPE.fullmatch(self.name):
            raise ValueError(f"{self.name!r} is not finance.<noun>.<verb>")
        if not callable(self.handler):
            raise ValueError(f"{self.name}: the handler is not callable")
        if not self.roles:
            raise ValueError(f"{self.name}: lists no role")
        if not self.description.strip():
            raise ValueError(f"{self.name}: has no description")
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
    role and an input its model rejects, all before any connection is opened. Then it
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

    with ctx.databases.engine(spec.connection).connect() as connection:
        transaction = connection.begin()
        try:
            result = spec.handler(ctx, UnitOfWork(connection), data)
            if not transaction.is_active:
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
