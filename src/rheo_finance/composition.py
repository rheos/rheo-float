"""The production ``FinanceContext``: what a command or a job dispatches with.

Settings from ``$FINANCE_DATA_DIR/settings.toml`` (defaults when it is missing), the
system clock, the owner and reader DSNs from ``db.env``, and the ``claude -p`` model
client behind the prompt guard, whose protected terms come from the manual accounts
files. Building one opens no connection, reads no manual file and starts no process:
``dispatch`` connects per operation, and the guard reads its terms on first use.
"""

from __future__ import annotations

from rheo_finance import db, operations
from rheo_finance.clock import SystemClock
from rheo_finance.context import FinanceContext, FinancePaths, SnapshotProvider
from rheo_finance.contracts import Role
from rheo_finance.derive.snapshot import txn_snapshot
from rheo_finance.model.claude_cli import ClaudeCLIClient
from rheo_finance.model.guard import PromptGuard, manual_accounts_terms
from rheo_finance.settings import load_settings, settings_path


def build_context(
    role: Role, *, snapshot_provider: SnapshotProvider = txn_snapshot
) -> FinanceContext:
    data = db.data_dir()
    settings = load_settings(settings_path(data))
    ctx = FinanceContext(
        settings=settings,
        clock=SystemClock(),
        paths=FinancePaths(data=data),
        role=role,
        snapshot_provider=snapshot_provider,
        model_client=ClaudeCLIClient(
            PromptGuard([manual_accounts_terms(data)]), settings
        ),
    )
    return operations.with_databases(
        ctx, owner_url=db.owner_url(), reader_url=db.reader_url()
    )
