"""What an operation runs with: ``FinanceContext`` and ``UnitOfWork``.

Local stand-ins for rheoStream's ``rheo_contracts.WorkspaceContext`` and
``rheo_core.refs.resolver.UnitOfWork``, so moving the operations into rheoStream later
is a move, not a rewrite. ``operations.dispatch`` is the only code that opens a
connection; a handler gets the ``UnitOfWork`` dispatch opened and never ends its
transaction itself.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict
from sqlalchemy import Connection, Engine, create_engine
from sqlalchemy.engine import URL
from sqlalchemy.pool import NullPool

from rheo_finance.clock import Clock
from rheo_finance.contracts import Role
from rheo_finance.settings import Settings

ConnectionName = Literal["owner", "reader"]


class ModelClient(Protocol):
    """The model step's client. Prompt 36 declares its methods; tests pass a stub."""


class LineSnapshot(BaseModel):
    """What a line looked like when a decision named it (Key decision 19)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    account_ref: str
    amount: Decimal
    currency: str
    posted_on: dt.date


class UnitOfWork:
    """The one transaction ``dispatch`` opened for an operation, as a handler sees it.

    It offers the connection's ``execute``, ``scalar`` and ``scalars`` and nothing else,
    so a handler has no ``commit``, ``rollback`` or ``close`` to end the transaction
    with: ending it is dispatch's job. A ``COMMIT`` sent as SQL cannot be stopped from
    here; dispatch detects it and raises.
    """

    __slots__ = ("execute", "scalar", "scalars")

    def __init__(self, connection: Connection) -> None:
        # The bound methods keep SQLAlchemy's typed overloads.
        self.execute = connection.execute
        self.scalar = connection.scalar
        self.scalars = connection.scalars


# A line reference to its snapshot, or None when no current line has that reference.
# Prompt 21 wires the real one over ``txn``; tests pass a fixture one.
SnapshotProvider = Callable[[UnitOfWork, str], LineSnapshot | None]


@dataclass(frozen=True)
class FinancePaths:
    """Filesystem locations outside the repo. ``data`` is ``$FINANCE_DATA_DIR``."""

    data: Path


class Databases:
    """The owner and reader DSNs, one engine per DSN, created on first use."""

    def __init__(self, owner_url: URL, reader_url: URL) -> None:
        self._urls: dict[ConnectionName, URL] = {
            "owner": owner_url,
            "reader": reader_url,
        }
        self._engines: dict[ConnectionName, Engine] = {}

    def url(self, name: ConnectionName) -> URL:
        return self._urls[name]

    def engine(self, name: ConnectionName) -> Engine:
        engine = self._engines.get(name)
        if engine is None:
            # NullPool: a CLI call or a job tick opens few connections, and a pooled
            # connection would outlive the role and grant state it was opened under.
            engine = create_engine(self._urls[name], poolclass=NullPool)
            self._engines[name] = engine
        return engine

    def dispose(self) -> None:
        for engine in self._engines.values():
            engine.dispose()
        self._engines.clear()


@dataclass(frozen=True)
class FinanceContext:
    """Everything a handler may use besides its input and its unit of work.

    ``role`` is the actor the context was built for; ``dispatch`` refuses a call made
    for a different role.
    """

    settings: Settings
    clock: Clock
    paths: FinancePaths
    role: Role
    databases: Databases
    snapshot_provider: SnapshotProvider
    model_client: ModelClient | None = field(default=None)
