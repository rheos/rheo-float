"""The ledger operations: ``finance.ledger.derive``, ``finance.ledger.rebuild`` and
``finance.ledger.digest`` (FR-5, SPEC "Operation catalogue").

- ``derive`` parses the documents whose stored parse output is missing or out of date,
  then runs every later stage (``derive.run``).
- ``rebuild`` registers the archive again (``documents.registry.file_archive``, the
  walk alone: it reads filed documents only, never Downloads), parses every document
  again, then runs every later stage. A parser fix reaches all history this way.
- ``digest`` hashes the derived tables (``derive.digest``). READ on the owner
  connection: it reads base tables, so it lists no ``member`` and names no tool (Key
  decision 21).
- ``finance.statement.list`` returns, for the documents the named parsers read, each
  document's status, rejection reason, period and the totals of its statement periods
  (opening, closing, debits, credits, balance due, due date). It is what the local
  parity check (``scripts/parity_check.py``, AC-7) compares with the existing parsers'
  outputs. READ on the owner connection, owner only, no tool: the totals are
  restricted. A ``superseded`` copy is left out (its twin is the parsed one); a
  document no parser read (``unparsed``) has no parser and is not listed.

Neither derive nor rebuild calls a model or the network: the stages read stored
proposals and rates, never fetch them. The handlers take the model client out of the
context before any stage runs, and nothing they import reaches ``rheo_finance.http``
(an import-graph test pins it). Both return the input fingerprint they computed before
writing (``derive.fingerprint``); the tick's dirty check that skips an unchanged
derive arrives with ``job_run`` (prompt 66), so here a derive always runs when asked.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
from collections import Counter, defaultdict
from decimal import Decimal
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from rheo_finance.context import FinanceContext, UnitOfWork
from rheo_finance.contracts import Role, SafetyClass, Tiered
from rheo_finance.derive import run as derive_run
from rheo_finance.derive.digest import ledger_digest
from rheo_finance.derive.fingerprint import fingerprint
from rheo_finance.documents.registry import ARCHIVE_KEY, DocumentRefused, file_archive
from rheo_finance.operations import OperationSpec, register
from rheo_finance.parsers import docextract_adapter
from rheo_finance.storage.schema import document, statement_period
from rheo_finance.tier import Tier

P = Tier.PUBLIC
R = Tier.RESTRICTED


class LedgerIn(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Count(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: Annotated[str, Tiered(P)]
    count: Annotated[int, Tiered(P)]


class DeriveOut(BaseModel):
    """Counts only."""

    model_config = ConfigDict(frozen=True)

    # Documents by status after the run.
    documents: Annotated[list[Count], Tiered(P)]
    # Documents parsed in this run, and documents whose stored parse output was kept.
    parsed_now: Annotated[int, Tiered(P)]
    kept: Annotated[int, Tiered(P)]
    txns: Annotated[int, Tiered(P)]
    additional_sources: Annotated[int, Tiered(P)]
    discrepancies: Annotated[int, Tiered(P)]
    incomplete_lines: Annotated[int, Tiered(P)]
    # Account-months by coverage state.
    months: Annotated[list[Count], Tiered(P)]
    # The input fingerprint taken before the run wrote anything.
    fingerprint: Annotated[str, Tiered(P)]


class RebuildOut(DeriveOut):
    # New documents the archive walk registered first.
    registered: Annotated[int, Tiered(P)]


class DigestOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    digest: Annotated[str, Tiered(P)]
    # Rows per digested table.
    tables: Annotated[list[Count], Tiered(P)]


def _counts(counter: Counter[str]) -> list[Count]:
    return [Count(name=name, count=count) for name, count in sorted(counter.items())]


def _offline(ctx: FinanceContext) -> FinanceContext:
    """``ctx`` with no model client: no stage of a derive or a rebuild can call one."""
    return dataclasses.replace(ctx, model_client=None)


def _derived(out: derive_run.Derived, taken: str) -> dict[str, object]:
    return {
        "documents": _counts(out.documents),
        "parsed_now": out.parsed_now,
        "kept": out.kept,
        "txns": out.lines.txns,
        "additional_sources": out.lines.additional_sources,
        "discrepancies": out.lines.discrepancies,
        "incomplete_lines": out.lines.incomplete_lines,
        "months": _counts(Counter({str(k): v for k, v in out.months.items()})),
        "fingerprint": taken,
    }


def derive(ctx: FinanceContext, uow: UnitOfWork, _data: LedgerIn) -> DeriveOut:
    ctx = _offline(ctx)
    taken = fingerprint(uow, ctx).digest
    out = derive_run.run(uow, ctx, extract_pdf=docextract_adapter.pdf_text)
    return DeriveOut.model_validate(_derived(out, taken))


def rebuild(ctx: FinanceContext, uow: UnitOfWork, _data: LedgerIn) -> RebuildOut:
    ctx = _offline(ctx)
    root = ctx.settings.get_path(ARCHIVE_KEY)
    if root is None or not root.is_dir():
        raise DocumentRefused(f"the {ARCHIVE_KEY} directory does not exist")
    taken = fingerprint(uow, ctx).digest
    filed = file_archive(uow, root, ctx.paths.data, ctx.clock.now_utc())
    out = derive_run.run(
        uow, ctx, extract_pdf=docextract_adapter.pdf_text, reparse=True
    )
    return RebuildOut.model_validate(
        {**_derived(out, taken), "registered": filed.registered}
    )


def digest(_ctx: FinanceContext, uow: UnitOfWork, _data: LedgerIn) -> DigestOut:
    found = ledger_digest(uow)
    return DigestOut(
        digest=found.digest,
        tables=[Count(name=name, count=n) for name, n in sorted(found.counts.items())],
    )


# --- finance.statement.list --------------------------------------------------------


class StatementListIn(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    # Parser ids ("vancity.statement", "virgin_plus.bill").
    parsers: Annotated[tuple[str, ...], Field(min_length=1)]


class PeriodTotals(BaseModel):
    model_config = ConfigDict(frozen=True)

    opening: Decimal | None
    closing: Decimal | None
    total_debits: Decimal | None
    total_credits: Decimal | None
    balance_due: Decimal | None
    due_date: dt.date | None


class StatementDocument(BaseModel):
    model_config = ConfigDict(frozen=True)

    parser_id: Annotated[str, Tiered(P)]
    status: Annotated[str, Tiered(P)]
    reject_reason: Annotated[str | None, Tiered(P)]
    period_start: Annotated[dt.date | None, Tiered(P)]
    period_end: Annotated[dt.date | None, Tiered(P)]
    # One entry per statement period, no account: the parity check compares them as
    # a multiset per statement.
    periods: Annotated[list[PeriodTotals], Tiered(R)]


class StatementListOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    documents: Annotated[list[StatementDocument], Tiered(P)]


def statement_list(
    _ctx: FinanceContext, uow: UnitOfWork, data: StatementListIn
) -> StatementListOut:
    docs = uow.execute(
        select(
            document.c.id,
            document.c.parser_id,
            document.c.status,
            document.c.reject_reason,
            document.c.period_start,
            document.c.period_end,
        )
        .where(
            document.c.parser_id.in_(data.parsers),
            document.c.status != "superseded",
        )
        .order_by(document.c.period_end, document.c.id)
    ).all()
    sp = statement_period.c
    periods: dict[object, list[PeriodTotals]] = defaultdict(list)
    rows = uow.execute(
        select(
            sp.document_id, sp.opening, sp.closing, sp.total_debits,
            sp.total_credits, sp.balance_due, sp.due_date,
        )
        .where(sp.document_id.in_([each.id for each in docs]))
        .order_by(sp.document_id, sp.account_id, sp.currency)
    )  # fmt: skip
    for row in rows:
        periods[row.document_id].append(
            PeriodTotals(
                opening=row.opening,
                closing=row.closing,
                total_debits=row.total_debits,
                total_credits=row.total_credits,
                balance_due=row.balance_due,
                due_date=row.due_date,
            )
        )
    return StatementListOut(
        documents=[
            StatementDocument(
                parser_id=each.parser_id,
                status=each.status,
                reject_reason=each.reject_reason,
                period_start=each.period_start,
                period_end=each.period_end,
                periods=periods.get(each.id, []),
            )
            for each in docs
        ]
    )


register(
    OperationSpec(
        name="finance.ledger.derive",
        input_model=LedgerIn,
        output_model=DeriveOut,
        handler=derive,
        safety_class=SafetyClass.MUTATE,
        roles=frozenset({Role.OWNER, Role.SERVICE}),
        connection="owner",
        description=(
            "Parse new or changed documents, keep the stored parse of the rest, and"
            " rebuild every derived table from them, the manual files and the"
            " decisions. Calls no model and no network."
        ),
    )
)
register(
    OperationSpec(
        name="finance.ledger.rebuild",
        input_model=LedgerIn,
        output_model=RebuildOut,
        handler=rebuild,
        safety_class=SafetyClass.MUTATE,
        roles=frozenset({Role.OWNER, Role.SERVICE}),
        connection="owner",
        description=(
            "Register the archive again, parse every document again and rebuild every"
            " derived table. Decisions are kept. Calls no model and no network."
        ),
    )
)
register(
    OperationSpec(
        name="finance.ledger.digest",
        input_model=LedgerIn,
        output_model=DigestOut,
        handler=digest,
        safety_class=SafetyClass.READ,
        roles=frozenset({Role.OWNER, Role.SERVICE}),
        connection="owner",
        description=(
            "The sha256 of the canonical rows of every derived table, timestamps left"
            " out, with the row count of each."
        ),
    )
)
register(
    OperationSpec(
        name="finance.statement.list",
        input_model=StatementListIn,
        output_model=StatementListOut,
        handler=statement_list,
        safety_class=SafetyClass.READ,
        roles=frozenset({Role.OWNER}),
        connection="owner",
        description=(
            "For the documents the named parsers read: status, rejection reason,"
            " period and the totals of each statement period. Leaves out superseded"
            " copies."
        ),
    )
)
