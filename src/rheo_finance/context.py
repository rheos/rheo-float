"""What an operation runs with: ``FinanceContext`` and ``UnitOfWork``.

Local stand-ins for rheoStream's ``rheo_contracts.WorkspaceContext`` and
``rheo_core.refs.resolver.UnitOfWork``, so moving the operations into rheoStream later
is a move, not a rewrite. ``operations.dispatch`` is the only code that opens a
connection; a handler gets the ``UnitOfWork`` dispatch opened and never ends its
transaction itself.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ConfigDict
from sqlalchemy import Connection, CursorResult, Executable, ScalarResult

from rheo_finance.clock import Clock
from rheo_finance.contracts import Role
from rheo_finance.model.client import ModelClient
from rheo_finance.settings import Settings

if TYPE_CHECKING:
    from rheo_finance.operations import _Databases

ConnectionName = Literal["owner", "reader"]


class LineSnapshot(BaseModel):
    """What a line looked like when a decision named it (Key decision 19).

    The fields are the ``decision_target`` columns of the same names.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    account_slug: str
    amount: Decimal
    currency: str
    posting_date: dt.date


_Params = Mapping[str, Any] | Sequence[Mapping[str, Any]]


class UnitOfWork:
    """The one transaction ``dispatch`` opened for an operation, as a handler sees it.

    It offers ``execute``, ``scalar`` and ``scalars`` on that transaction's connection,
    and ``savepoint`` for a step whose failure must not undo the rest, and nothing
    else, so a handler has no ``commit``, ``rollback`` or ``close`` to end the
    transaction with: ending it is dispatch's job. A ``COMMIT`` sent as SQL cannot
    be stopped from here; dispatch detects it and raises.
    """

    __slots__ = ("_connection",)

    def __init__(self, connection: Connection) -> None:
        self._connection = connection

    def execute(
        self, statement: Executable, parameters: _Params | None = None
    ) -> CursorResult[*tuple[Any, ...]]:
        return self._connection.execute(statement, parameters)

    def scalar(
        self, statement: Executable, parameters: Mapping[str, Any] | None = None
    ) -> Any:
        return self._connection.scalar(statement, parameters)

    def scalars(
        self, statement: Executable, parameters: _Params | None = None
    ) -> ScalarResult[Any]:
        return self._connection.scalars(statement, parameters)

    @contextmanager
    def savepoint(self) -> Iterator[None]:
        """Run a block in a savepoint: an error inside rolls back only the block, and
        is re-raised; the transaction around it stays open either way."""
        nested = self._connection.begin_nested()
        try:
            yield
        except BaseException:
            nested.rollback()
            raise
        nested.commit()


# A line reference to its snapshot, or None when no current line has that reference.
# Prompt 21 wires the real one over ``txn``; tests pass a fixture one.
SnapshotProvider = Callable[[UnitOfWork, str], LineSnapshot | None]


@dataclass(frozen=True)
class FinancePaths:
    """Filesystem locations outside the repo. ``data`` is ``$FINANCE_DATA_DIR``."""

    data: Path


@dataclass(frozen=True)
class FinanceContext:
    """Everything a handler may use besides its input and its unit of work.

    ``role`` is the actor the context was built for; ``dispatch`` refuses a call made
    for a different role. The DSNs are not a handler's to use:
    ``operations.with_databases`` puts them in ``_databases``, which only ``dispatch``
    reads, and the context a handler receives has it cleared, so a handler can neither
    open another connection nor dispatch again.
    """

    settings: Settings
    clock: Clock
    paths: FinancePaths
    role: Role
    snapshot_provider: SnapshotProvider
    model_client: ModelClient | None = field(default=None)
    _databases: _Databases | None = field(default=None, repr=False, compare=False)
