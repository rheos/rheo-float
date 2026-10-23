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

Neither derive nor rebuild calls a model or the network: the stages read stored
proposals and rates, never fetch them. The handlers take the model client out of the
context before any stage runs, and nothing they import reaches ``rheo_finance.http``
(an import-graph test pins it). Both return the input fingerprint they computed before
writing (``derive.fingerprint``); the tick's dirty check that skips an unchanged
derive arrives with ``job_run`` (prompt 66), so here a derive always runs when asked.
"""

from __future__ import annotations

import dataclasses
from collections import Counter
from typing import Annotated

from pydantic import BaseModel, ConfigDict

from rheo_finance.context import FinanceContext, UnitOfWork
from rheo_finance.contracts import Role, SafetyClass, Tiered
from rheo_finance.derive import run as derive_run
from rheo_finance.derive.digest import ledger_digest
from rheo_finance.derive.fingerprint import fingerprint
from rheo_finance.documents.registry import ARCHIVE_KEY, DocumentRefused, file_archive
from rheo_finance.operations import OperationSpec, register
from rheo_finance.parsers import docextract_adapter
from rheo_finance.tier import Tier

P = Tier.PUBLIC


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
