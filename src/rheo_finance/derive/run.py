"""``run``: rebuild every derived table in one transaction (FR-5, Key decisions 3
and 4). The run is a savepoint of the caller's transaction, so a derive that fails
leaves every table as it found it, whatever the caller does next.

``finance.ledger.derive`` calls it as is; ``finance.ledger.rebuild`` registers the
archive again first and passes ``reparse``. Both give the same ledger for the same
document set: a derive keeps only stored parse output that a parse would reproduce
(``stage1``), and everything after stage 1 is recomputed from the stored parse output
either way.

Order of work, set by what each step reads:

1. Clear the derived tables that hold rows keyed to accounts, lines and documents: the
   coverage matrix, the pairing graph, the ``txn`` tables, the source discrepancies and
   the events. The
   parse cache (``PARSE_CACHE``: lines, periods, rejected periods and the rejection
   events) is cleared whole with ``reparse``; otherwise stage 1 clears and rewrites a
   document's rows when it parses that document, and the columns stage 3 writes on a
   line (``STAGE3_LINE_COLUMNS``) go back to null, as a parse leaves them. State
   tables (decisions and their snapshots) are never touched, and neither are the
   registry tables the file operation owns (``document``, ``archive_skip``).
2. Stage 2, the manual files: accounts and identifiers first. The parsers match
   accounts from the account table, so this comes before stage 1 although its number is
   higher. The loaders rewrite their own tables.
3. Stage 1, the registered documents through the parser registry, each one only when
   its stored parse is missing or out of date (all of them with ``reparse``). A
   document whose account left the registry is parsed again against the new one, so
   no stale account key reaches the foreign-key check. Then repeated copies of one
   document are ``superseded`` (``precedence.supersede``).
4. Stage 3, lines to txns: documents ranked per account, the overlap matcher, the
   source discrepancies (``stage3``, ``precedence``, ``overlap``), then the receipts
   and invoices linked to the txns as evidence (``evidence``).
5. Stage 4, conversion to CAD from the stored rates (``stage4``); stage 5,
   recognition dates (``stage5``); then stage 6, the coverage matrix (``coverage``),
   which reads the periods, the lines and ``document_periods.csv``; then stage 7, the
   exclusions and the crypto movement kinds (``stage7``), which reads the own
   addresses stage 2 stored and the ``include`` decisions; then stage 8, the
   reversals in both directions (``stage8``), which reads the lines' reversal wording
   and the ``reversal`` rows of ``transfer_hints.csv``; then stage 9, the fiat own
   transfer and same-account conversion candidate edges (``stage9``), which reads the
   lines' transfer and conversion wording, the other rows of that file, the CAD values,
   the stored rates and the lines stage 8 paired, and the crypto own-wallet transfer
   edges (``crypto_edges``), which read the crypto kinds stage 7 set, the own
   addresses and the crypto labels; then stage 11, resolution (``stage11``): the
   owner's ``pair`` and ``unpair`` decisions apply first, then the
   mutually unique edges apply, every transfer-like line gets its state, a labelled
   crypto line is set aside, an unresolved crypto withdrawal is flagged, and an applied
   transfer's net or conversion's spread becomes a derived fee line.
6. Check the deferred constraints now, inside the run, so a violation fails the derive
   and not a commit nobody is watching; then defer them again, so a second derive in
   the same transaction clears the txn tables as the first did.
7. Stage 16, the sink specs (``sinks.store.write_sink_specs``), last.

A stage is a plain import at its step; a feature package that is not built yet has no
call site (Key decision 14).
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from sqlalchemy import delete, text, update

from rheo_finance.clock import local_today
from rheo_finance.context import FinanceContext, UnitOfWork
from rheo_finance.derive import (
    coverage,
    crypto_edges,
    stage1,
    stage2,
    stage3,
    stage4,
    stage5,
    stage7,
    stage8,
    stage9,
    stage11,
)
from rheo_finance.documents.registry import ARCHIVE_KEY, DocumentRefused
from rheo_finance.parsers.docextract_adapter import pdf_text
from rheo_finance.parsers.registry import DOCUMENT_REJECTED
from rheo_finance.sinks.store import write_sink_specs
from rheo_finance.storage.schema import (
    coverage as coverage_table,
)
from rheo_finance.storage.schema import (
    document_line,
    event,
    pair_edge,
    rejected_period,
    source_discrepancy,
    statement_period,
    txn,
    txn_source,
)

OVERLAP_DAYS_KEY: Final = "finance.import.overlap_day_tolerance"
RECEIPT_DAYS_KEY: Final = "finance.receipts.match_days"
INVOICE_DAYS_KEY: Final = "finance.invoices.match_days"
# Emptied before anything is rebuilt, children first.
CLEARED: Final = (
    coverage_table, pair_edge, txn_source, txn, source_discrepancy, document_line,
    statement_period, rejected_period, event,
)  # fmt: skip
# Of CLEARED, what stage 1 stores per document: kept for documents a derive does not
# parse again. ``event`` holds only the rejection events of it (``DOCUMENT_REJECTED``).
PARSE_CACHE: Final = frozenset({document_line, statement_period, rejected_period})
# Written on ``document_line`` by stage 3, null after a parse.
STAGE3_LINE_COLUMNS: Final = ("description_norm", "line_key_id")
# Rewritten whole by a loader or a stage.
REWRITTEN: Final = frozenset(
    {
        "account", "account_identifier", "manual_entry_error", "filed_year",
        "own_address", "crypto_label", "sink_spec",
    }
)  # fmt: skip
# State, never truncated (Key decision 3): the decisions, the stored rates and the
# ranges fetches covered. Each is an input of a derive and feeds a part of the input
# fingerprint (``fingerprint.STATE_PARTS``).
STATE: Final = frozenset(
    {"decision", "decision_item", "decision_target", "rate", "rate_coverage"}
)
# Owned by the file operation (``finance.document.file``), not by a derive.
FILE_OWNED: Final = frozenset({"document", "archive_skip"})


@dataclass(frozen=True)
class Derived:
    """Counts of what one derive did."""

    documents: Counter[str]
    lines: stage3.Lines
    # Account-months by coverage state.
    months: Counter[coverage.State]
    # Documents parsed in this run, and documents whose stored parse output was kept.
    parsed_now: int = 0
    kept: int = 0


def _clear(uow: UnitOfWork, *, reparse: bool) -> None:
    for table in CLEARED:
        if reparse or (table not in PARSE_CACHE and table is not event):
            uow.execute(delete(table))
    if not reparse:
        # Stage 1 owns the rejection events, per document; every other event is
        # derived after it.
        uow.execute(delete(event).where(event.c.type != DOCUMENT_REJECTED))
        uow.execute(
            update(document_line).values(
                {column: None for column in STAGE3_LINE_COLUMNS}
            )
        )


def run(
    uow: UnitOfWork,
    ctx: FinanceContext,
    *,
    extract_pdf: Callable[[Path], str] = pdf_text,
    reparse: bool = False,
) -> Derived:
    """Rebuild every derived table from the archive, the manual files and the
    decisions; with ``reparse``, parse every document again instead of keeping the
    stored parse output that is still current. Raises ``DocumentRefused`` before
    changing anything when the archive directory does not exist (an unmounted drive
    must not empty the ledger)."""
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
        _clear(uow, reparse=reparse)

        manual = stage2.load_manual(uow, ctx.paths.data)
        parsing = stage1.parse_all(
            uow, archive, extract_pdf=extract_pdf, reparse=reparse
        )
        currencies = {row.slug: set(row.currencies) for row in manual.registry.accounts}
        lines = stage3.build_lines(
            uow,
            manual.cash_lines,
            currencies,
            zone=ctx.settings.timezone,
            overlap_days=ctx.settings.get_int(OVERLAP_DAYS_KEY),
            receipt_days=ctx.settings.get_int(RECEIPT_DAYS_KEY),
            invoice_days=ctx.settings.get_int(INVOICE_DAYS_KEY),
            qty_epsilon=ctx.settings.get_decimal(crypto_edges.EPSILON_KEY),
        )

        stage4.convert(uow)
        stage5.recognize(uow)
        today = local_today(ctx.clock, ctx.settings.timezone)
        months = coverage.build_coverage(uow, manual.document_periods.periods, today)
        stage7.attribute(uow)
        stage8.pair_reversals(
            uow,
            window_days=ctx.settings.get_int(stage8.WINDOW_KEY),
            hint_rows=manual.transfer_hints.rows,
        )
        stage9.pair_transfers(
            uow,
            tolerance=stage9.Tolerance(
                window_days=ctx.settings.get_int(stage9.WINDOW_KEY),
                abs_cad=ctx.settings.get_decimal(stage9.ABS_KEY),
                pct=ctx.settings.get_decimal(stage9.PCT_KEY),
            ),
            spread=stage9.Spread(
                window_days=ctx.settings.get_int(stage9.CONVERSION_WINDOW_KEY),
                pct=ctx.settings.get_decimal(stage9.SPREAD_KEY),
            ),
            hint_rows=manual.transfer_hints.rows,
        )
        crypto_edges.pair_crypto(
            uow,
            tolerance=crypto_edges.Tolerance(
                window_hours=ctx.settings.get_int(crypto_edges.WINDOW_KEY),
                fee_pct=ctx.settings.get_decimal(crypto_edges.FEE_PCT_KEY),
                epsilon=ctx.settings.get_decimal(crypto_edges.EPSILON_KEY),
            ),
        )
        stage11.resolve(uow)

        uow.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
        uow.execute(text("SET CONSTRAINTS ALL DEFERRED"))
        write_sink_specs(uow)
    return Derived(
        documents=parsing.outcomes,
        lines=lines,
        months=months,
        parsed_now=parsing.parsed_now,
        kept=parsing.kept,
    )
