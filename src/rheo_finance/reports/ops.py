"""The reader operations behind ``status``, ``coverage`` and the document list:
``finance.status.get``, ``finance.coverage.get`` and ``finance.document.list`` (FR-48,
FR-51, Key decision 21).

All three are READ on the reader connection and select from ``report_*`` views only, so
the reader role's grants are what keeps a base table or a restricted column out of
them.

``finance.status.get`` returns the status lines that exist so far as ``(line_no,
text)``. The numbering is SPEC's and stays fixed; lines 5, 6, 8 and 11 to 14 arrive
with later prompts, so a gap in the numbers is expected.

1. as of, and the database state: ``database current`` when ``report_status``'s schema
   revision is the code's head, else what it is at and the fix;
2. the last import per top-level archive directory, on one line, as local dates;
3. unparsed and rejected documents (and those that need manual entry, when any);
4. missing account-months;
7. sinks: ``sinks off: <names>`` naming every sink that is not enabled, and any whose
   approval is stale, or ``sinks: all enabled``. It is ``finance.sink.state``'s own
   reading: ``report_sink`` and ``report_sink_spec``, and the ``enabled`` setting from
   ``$FINANCE_DATA_DIR/settings.toml`` as the gate reads it;
9. data checks, only when one applies: source discrepancies, accounts with no data, and
   archive files skipped because their name carries an identifier (by directory;
   other skip reasons are not fixed by a rename and are not counted here);
10. intake, only when it applies: monitor-pattern files waiting in Downloads (the
   filer's count, read from the directory's names and mtimes).

Nothing here prints an identifier, a description, a merchant, a counterparty or a
skipped file's path: the views carry none of them.
"""

from __future__ import annotations

import datetime as dt
from collections import defaultdict
from dataclasses import dataclass
from typing import Annotated, Final

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from rheo_finance.clock import local_date
from rheo_finance.context import FinanceContext, UnitOfWork
from rheo_finance.contracts import Role, SafetyClass, Tiered
from rheo_finance.db import head_revision
from rheo_finance.documents.filer import DOWNLOADS_KEY, monitor_files_waiting
from rheo_finance.operations import OperationSpec, register
from rheo_finance.render import as_of
from rheo_finance.reports import views
from rheo_finance.sinks.ops import StateIn
from rheo_finance.sinks.ops import state as sink_state
from rheo_finance.storage.schema import report_coverage, report_document, report_status
from rheo_finance.tier import Tier

P = Tier.PUBLIC
READERS: Final = frozenset({Role.OWNER, Role.MEMBER, Role.SERVICE})


@dataclass(frozen=True)
class Fact:
    subject: str | None
    count: int
    at: dt.datetime | None


def _plural(count: int, one: str, many: str) -> str:
    return f"{count} {one if count == 1 else many}"


def _facts(uow: UnitOfWork) -> dict[str, list[Fact]]:
    found: dict[str, list[Fact]] = defaultdict(list)
    status_view = report_status.c
    rows = uow.execute(
        select(status_view.fact, status_view.subject, status_view.count, status_view.at)
    )
    for fact, subject, count, at in rows:
        found[fact].append(Fact(subject, int(count), at))
    return found


# --- finance.status.get ---------------------------------------------------------------


class StatusIn(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class StatusLine(BaseModel):
    model_config = ConfigDict(frozen=True)

    line_no: Annotated[int, Tiered(P)]
    text: Annotated[str, Tiered(P)]


class StatusOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    lines: Annotated[list[StatusLine], Tiered(P)]


def _database(ctx: FinanceContext, facts: dict[str, list[Fact]]) -> str:
    stamp = as_of(ctx.clock, ctx.settings.timezone)
    at = sorted(each.subject or "" for each in facts[views.SCHEMA])
    head = head_revision()
    if at == [head]:
        return f"{stamp}, database current"
    revision = ", ".join(at) or "no revision"
    return (
        f"{stamp}, database schema at {revision}, code at {head}:"
        " run finance db migrate"
    )


def _imports(ctx: FinanceContext, facts: dict[str, list[Fact]]) -> str:
    zone = ctx.settings.timezone
    last = sorted(
        (each.subject or "", local_date(each.at, zone).isoformat())
        for each in facts[views.LAST_IMPORT]
        if each.at is not None
    )
    if not last:
        return "last import: none"
    return "last import: " + ", ".join(f"{name} {day}" for name, day in last)


def _documents(facts: dict[str, list[Fact]]) -> str:
    counts = {each.subject: each.count for each in facts[views.DOCUMENT_STATUS]}
    text = (
        f"documents: {counts.get('unparsed', 0)} unparsed,"
        f" {counts.get('rejected', 0)} rejected"
    )
    manual = counts.get("needs_manual_entry", 0)
    if manual:
        text += f", {manual} need{'s' if manual == 1 else ''} manual entry"
    return text


def _missing(facts: dict[str, list[Fact]]) -> str:
    accounts = facts[views.MISSING_MONTHS]
    total = sum(each.count for each in accounts)
    if not total:
        return "missing account-months: none"
    return (
        f"missing account-months: {total} across"
        f" {_plural(len(accounts), 'account', 'accounts')}"
    )


def _sinks(ctx: FinanceContext, uow: UnitOfWork) -> str:
    sinks = sink_state(ctx, uow, StateIn()).sinks
    off = [each.sink for each in sinks if not each.enabled]
    if not off:
        return "sinks: all enabled"
    stale = [each.sink for each in sinks if each.approval == "stale"]
    text = "sinks off: " + " ".join(off)
    if stale:
        text += "; approval stale: " + " ".join(stale)
    return text


def _data_checks(facts: dict[str, list[Fact]]) -> str | None:
    parts: list[str] = []
    discrepancies = sum(each.count for each in facts[views.SOURCE_DISCREPANCY])
    if discrepancies:
        parts.append(
            _plural(discrepancies, "source discrepancy", "source discrepancies")
        )
    no_data = sorted(each.subject or "" for each in facts[views.ACCOUNT_NO_DATA])
    if no_data:
        parts.append(
            _plural(len(no_data), "account", "accounts")
            + " with no data: "
            + ", ".join(no_data)
        )
    skipped = sorted(
        (each.subject or "", each.count)
        for each in facts[views.ARCHIVE_SKIP_IDENTIFIER]
        if each.count
    )
    total = sum(count for _, count in skipped)
    if total:
        where = ", ".join(f"{directory} {count}" for directory, count in skipped)
        parts.append(
            _plural(total, "archive file", "archive files")
            + f" skipped: file name carries an identifier, rename them ({where})"
        )
    return "; ".join(parts) if parts else None


def _intake(ctx: FinanceContext) -> str | None:
    downloads = ctx.settings.get_path(DOWNLOADS_KEY)
    waiting = monitor_files_waiting(downloads, ctx.clock.now_utc())
    if not waiting:
        return None
    return _plural(waiting, "monitor file", "monitor files") + " waiting in Downloads"


def status(ctx: FinanceContext, uow: UnitOfWork, _data: StatusIn) -> StatusOut:
    facts = _facts(uow)
    lines: list[tuple[int, str | None]] = [
        (1, _database(ctx, facts)),
        (2, _imports(ctx, facts)),
        (3, _documents(facts)),
        (4, _missing(facts)),
        (7, _sinks(ctx, uow)),
        (9, _data_checks(facts)),
        (10, _intake(ctx)),
    ]
    return StatusOut(
        lines=[
            StatusLine(line_no=number, text=text)
            for number, text in lines
            if text is not None
        ]
    )


# --- finance.coverage.get -------------------------------------------------------------


class CoverageIn(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    # An account slug; None for every account.
    account: str | None = None
    year: Annotated[int, Field(ge=1900, le=2999)] | None = None


class CoverageMonth(BaseModel):
    model_config = ConfigDict(frozen=True)

    account: Annotated[str, Tiered(P)]
    account_label: Annotated[str, Tiered(P)]
    month: Annotated[dt.date, Tiered(P)]
    state: Annotated[str, Tiered(P)]
    # The reason in words ("running balance break"), or None.
    reason: Annotated[str | None, Tiered(P)]


class CoverageOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    months: Annotated[list[CoverageMonth], Tiered(P)]
    # Accounts with no coverage month at all, whatever the filters.
    no_data_accounts: Annotated[int, Tiered(P)]


def coverage_get(
    _ctx: FinanceContext, uow: UnitOfWork, data: CoverageIn
) -> CoverageOut:
    query = select(report_coverage).order_by(
        report_coverage.c.account, report_coverage.c.month
    )
    if data.account is not None:
        query = query.where(report_coverage.c.account == data.account)
    if data.year is not None:
        query = query.where(
            report_coverage.c.month >= dt.date(data.year, 1, 1),
            report_coverage.c.month <= dt.date(data.year, 12, 31),
        )
    months = [
        CoverageMonth(
            account=row.account,
            account_label=row.account_label,
            month=row.month,
            state=row.state,
            reason=row.reason,
        )
        for row in uow.execute(query)
    ]
    no_data = len(_facts(uow)[views.ACCOUNT_NO_DATA])
    return CoverageOut(months=months, no_data_accounts=no_data)


# --- finance.document.list ------------------------------------------------------------


class DocumentListIn(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class DocumentRow(BaseModel):
    model_config = ConfigDict(frozen=True)

    path: Annotated[str, Tiered(P)]
    kind: Annotated[str, Tiered(P)]
    status: Annotated[str, Tiered(P)]
    period_start: Annotated[dt.date | None, Tiered(P)]
    period_end: Annotated[dt.date | None, Tiered(P)]
    official: Annotated[bool, Tiered(P)]
    extraction: Annotated[str, Tiered(P)]


class DocumentListOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    documents: Annotated[list[DocumentRow], Tiered(P)]


def document_list(
    _ctx: FinanceContext, uow: UnitOfWork, _data: DocumentListIn
) -> DocumentListOut:
    rows = uow.execute(select(report_document).order_by(report_document.c.path))
    return DocumentListOut(
        documents=[
            DocumentRow(
                path=row.path,
                kind=row.kind,
                status=row.status,
                period_start=row.period_start,
                period_end=row.period_end,
                official=row.official,
                extraction=row.extraction,
            )
            for row in rows
        ]
    )


register(
    OperationSpec(
        name="finance.status.get",
        input_model=StatusIn,
        output_model=StatusOut,
        handler=status,
        safety_class=SafetyClass.READ,
        roles=READERS,
        connection="reader",
        tool_name="finance_status",
        description=(
            "The status lines built so far, numbered as SPEC fixes them, from the"
            " report views."
        ),
    )
)
register(
    OperationSpec(
        name="finance.coverage.get",
        input_model=CoverageIn,
        output_model=CoverageOut,
        handler=coverage_get,
        safety_class=SafetyClass.READ,
        roles=READERS,
        connection="reader",
        tool_name="finance_coverage",
        description=(
            "The coverage matrix: account label, month, state and reason in words,"
            " optionally for one account slug or one year."
        ),
    )
)
register(
    OperationSpec(
        name="finance.document.list",
        input_model=DocumentListIn,
        output_model=DocumentListOut,
        handler=document_list,
        safety_class=SafetyClass.READ,
        roles=READERS,
        connection="reader",
        tool_name="finance_documents",
        description=(
            "Every registered document: path, kind, status, period, official and"
            " extraction."
        ),
    )
)
