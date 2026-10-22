"""The production ``FinanceContext``: what a command or a job dispatches with.

Settings from ``$FINANCE_DATA_DIR/settings.toml`` (defaults when it is missing), the
system clock, and the owner and reader DSNs from ``db.env``. Building one opens no
connection; ``dispatch`` does, per operation.
"""

from __future__ import annotations

from rheo_finance import db, operations
from rheo_finance.clock import SystemClock
from rheo_finance.context import (
    FinanceContext,
    FinancePaths,
    LineSnapshot,
    SnapshotProvider,
    UnitOfWork,
)
from rheo_finance.contracts import Role
from rheo_finance.settings import load_settings, settings_path


def no_current_lines(_uow: UnitOfWork, _ref: str) -> LineSnapshot | None:
    """No line resolves, so a line-keyed decision is refused rather than stored
    without its snapshot.

    shortcut: there is no line table yet; prompt 21 replaces this default with the
    provider over ``txn``.
    """
    return None


def build_context(
    role: Role, *, snapshot_provider: SnapshotProvider = no_current_lines
) -> FinanceContext:
    data = db.data_dir()
    ctx = FinanceContext(
        settings=load_settings(settings_path(data)),
        clock=SystemClock(),
        paths=FinancePaths(data=data),
        role=role,
        snapshot_provider=snapshot_provider,
    )
    return operations.with_databases(
        ctx, owner_url=db.owner_url(), reader_url=db.reader_url()
    )
