"""``run``: truncate every derived table and rebuild it, in one transaction (FR-5, Key
decision 3). The rebuild runs in a savepoint of the caller's transaction, so a derive
that fails leaves every table as it found it, whatever the caller does next.

Order of work, set by what each step reads:

1. Clear the derived tables that hold rows keyed to accounts, lines and documents: the
   ``txn`` tables, the parse cache and the events. State tables (decisions and their
   snapshots) are never touched, and neither are the registry tables the file operation
   owns (``document``, ``archive_skip``).
2. Stage 2, the manual files: accounts and identifiers first. The parsers match
   accounts from the account table, so this comes before stage 1 although its number is
   higher. The loaders rewrite their own tables.
3. Stage 1, every registered document through the parser registry. The parse cache was
   cleared in step 1, so a document whose account left the registry is parsed again
   against the new one and no stale account key reaches the foreign-key check.
4. Stage 3, lines to txns.
5. Check the deferred constraints now, inside the run, so a violation fails the derive
   and not a commit nobody is watching; then defer them again, so a second derive in
   the same transaction clears the txn tables as the first did.
6. Stage 16, the sink specs (``sinks.store.write_sink_specs``), last.

A stage is a plain import at its step; a feature package that is not built yet has no
call site (Key decision 14).
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from sqlalchemy import delete, text

from rheo_finance.context import FinanceContext, UnitOfWork
from rheo_finance.derive import stage1, stage2, stage3
from rheo_finance.documents.registry import ARCHIVE_KEY, DocumentRefused
from rheo_finance.parsers.docextract_adapter import pdf_text
from rheo_finance.sinks.store import write_sink_specs
from rheo_finance.storage.schema import (
    document_line,
    event,
    statement_period,
    txn,
    txn_source,
)

# Emptied before anything is rebuilt, children first.
CLEARED: Final = (txn_source, txn, document_line, statement_period, event)
# Rewritten whole by a loader or a stage.
REWRITTEN: Final = frozenset(
    {
        "account", "account_identifier", "manual_entry_error", "filed_year",
        "sink_spec",
    }
)  # fmt: skip
# State, never truncated (Key decision 3).
STATE: Final = frozenset({"decision", "decision_item", "decision_target"})
# Owned by the file operation (``finance.document.file``), not by a derive.
FILE_OWNED: Final = frozenset({"document", "archive_skip"})


@dataclass(frozen=True)
class Derived:
    """Counts of what one derive did."""

    documents: Counter[str]
    lines: stage3.Lines


def run(
    uow: UnitOfWork,
    ctx: FinanceContext,
    *,
    extract_pdf: Callable[[Path], str] = pdf_text,
) -> Derived:
    """Rebuild every derived table from the archive, the manual files and the
    decisions. Raises ``DocumentRefused`` before changing anything when the archive
    directory does not exist (an unmounted drive must not empty the ledger)."""
    archive = ctx.settings.get_path(ARCHIVE_KEY)
    if archive is None or not archive.is_dir():
        raise DocumentRefused(f"the {ARCHIVE_KEY} directory does not exist")

    # The whole rebuild is one savepoint: a failure at any step puts back every row the
    # derive cleared or rewrote, so a caller that records the failure and commits
    # commits the ledger as it was, never a half-cleared one.
    with uow.savepoint():
        # A caller's earlier check may have left the constraints immediate; clearing
        # the txn tables needs them deferred (a txn is briefly without its source).
        uow.execute(text("SET CONSTRAINTS ALL DEFERRED"))
        for table in CLEARED:
            uow.execute(delete(table))

        manual = stage2.load_manual(uow, ctx.paths.data)
        documents = stage1.parse_all(uow, archive, extract_pdf=extract_pdf)
        currencies = {row.slug: set(row.currencies) for row in manual.registry.accounts}
        lines = stage3.build_lines(uow, manual.cash_lines, currencies)

        uow.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
        uow.execute(text("SET CONSTRAINTS ALL DEFERRED"))
        write_sink_specs(uow)
    return Derived(documents=documents, lines=lines)
