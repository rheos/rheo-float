"""Read-time owed figures and evidence, exclusively from granted report views."""

from __future__ import annotations

import datetime as dt
from collections import defaultdict
from decimal import Decimal
from typing import Annotated

from pydantic import BaseModel, ConfigDict
from sqlalchemy import select

from rheo_finance.clock import local_date
from rheo_finance.context import FinanceContext, UnitOfWork
from rheo_finance.contracts import Role, SafetyClass, Tiered
from rheo_finance.operations import OperationSpec, register
from rheo_finance.render import as_of
from rheo_finance.reports.gaps import bill_issuer
from rheo_finance.reports.open_items import ListIn as CreditIn
from rheo_finance.reports.open_items import OpenItemRow, list_items
from rheo_finance.storage.schema import report_document, report_owed, report_owed_trace
from rheo_finance.tier import Tier

P, R = Tier.PUBLIC, Tier.RESTRICTED
ZERO = Decimal("0.00")


class ListIn(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Facts(BaseModel):
    model_config = ConfigDict(frozen=True)
    obligation_ref: Annotated[str, Tiered(P)]
    payee_slug: Annotated[str, Tiered(P)]
    payee_label: Annotated[str, Tiered(P)]
    kind: Annotated[str, Tiered(P)]
    period_label: Annotated[str, Tiered(P)]
    statement_date: Annotated[dt.date | None, Tiered(P)]
    issued_on: Annotated[dt.date | None, Tiered(P)]
    amount_due: Annotated[Decimal, Tiered(R)]
    minimum_due: Annotated[Decimal | None, Tiered(R)]
    threshold_due: Annotated[Decimal, Tiered(R)]
    paid: Annotated[Decimal, Tiered(R)]
    remaining: Annotated[Decimal, Tiered(R)]
    threshold_remaining: Annotated[Decimal, Tiered(R)]
    due_date: Annotated[dt.date | None, Tiered(P)]
    status: Annotated[str, Tiered(P)]
    primary_source: Annotated[str, Tiered(P)]
    manual_source: Annotated[str | None, Tiered(P)]
    before_floor: Annotated[bool, Tiered(P)]
    payments_unmatchable: Annotated[bool, Tiered(P)]
    manual_mismatch: Annotated[Decimal | None, Tiered(R)]
    payments_mismatch: Annotated[Decimal | None, Tiered(R)]
    statement_conflict: Annotated[bool, Tiered(P)]
    invoice_ref: Annotated[str | None, Tiered(P)]
    arrangement_ref: Annotated[str | None, Tiered(P)]
    supersedes_ref: Annotated[str | None, Tiered(P)]
    presumed_paid_basis: Annotated[str | None, Tiered(P)]


class OwedRow(Facts):
    overdue: Annotated[Decimal, Tiered(R)]
    carried: Annotated[Decimal, Tiered(R)]
    due_in_window: Annotated[Decimal, Tiered(R)]
    days_until_due: Annotated[int | None, Tiered(P)]
    recorded_paid: Annotated[Decimal, Tiered(R)]
    recorded_remaining: Annotated[Decimal, Tiered(R)]
    recorded_threshold_remaining: Annotated[Decimal, Tiered(R)]
    reminder_eligible: Annotated[bool, Tiered(P)]
    invoice_folder: Annotated[str | None, Tiered(P)]


def figures(fact: Facts, today: dt.date, window: int) -> OwedRow:
    inactive = fact.status in {"paid", "presumed_paid", "superseded", "disputed"}
    remaining = ZERO if inactive else fact.remaining
    threshold = ZERO if inactive else fact.threshold_remaining
    paid = fact.amount_due if fact.status == "presumed_paid" else fact.paid
    days = None if fact.due_date is None else (fact.due_date - today).days
    overdue = threshold if days is not None and days < 0 else ZERO
    carried = remaining - threshold if fact.kind == "card_statement" else ZERO
    due = threshold if days is not None and 0 <= days <= window else ZERO
    # Folder keys are canonical archive keys, never the invoice's printed name.
    source = fact.primary_source.split("/")
    folder = (
        "/".join(source[:2])
        if len(source) >= 3 and source[0] == "invoices-received"
        else None
    )
    return OwedRow(
        **(
            fact.model_dump()
            | {"paid": paid, "remaining": remaining, "threshold_remaining": threshold}
        ),
        overdue=overdue,
        carried=carried,
        due_in_window=due,
        days_until_due=days,
        recorded_paid=fact.paid,
        recorded_remaining=fact.remaining,
        recorded_threshold_remaining=fact.threshold_remaining,
        reminder_eligible=not fact.before_floor
        and not inactive
        and days is not None
        and threshold > 0,
        invoice_folder=folder,
    )


class Gap(BaseModel):
    model_config = ConfigDict(frozen=True)
    path: Annotated[str, Tiered(P)]
    issuer_label: Annotated[str, Tiered(P)]
    kind: Annotated[str, Tiered(P)]
    status: Annotated[str, Tiered(P)]
    period_start: Annotated[dt.date | None, Tiered(P)]
    period_end: Annotated[dt.date | None, Tiered(P)]


def gaps(uow: UnitOfWork) -> list[Gap]:
    rows = uow.execute(
        select(report_document)
        .where(
            report_document.c.kind.in_(["bill", "image"]),
            report_document.c.status.in_(["needs_manual_entry", "unparsed"]),
            report_document.c.has_obligation.is_(False),
        )
        .order_by(report_document.c.path)
    )
    return [
        Gap(
            path=row.path,
            issuer_label=row.issuer,
            kind=row.kind,
            status=row.status,
            period_start=row.period_start,
            period_end=row.period_end,
        )
        for row in rows
        if bill_issuer(row.issuer)
    ]


class CreditSum(BaseModel):
    model_config = ConfigDict(frozen=True)
    currency: Annotated[str, Tiered(P)]
    amount: Annotated[Decimal, Tiered(R)]
    cad_amount: Annotated[Decimal | None, Tiered(R)]


class Credits(BaseModel):
    model_config = ConfigDict(frozen=True)
    count: Annotated[int, Tiered(P)]
    line: Annotated[str, Tiered(R)]
    sums: Annotated[list[CreditSum], Tiered(P)]
    items: Annotated[list[OpenItemRow], Tiered(P)]


def credits(ctx: FinanceContext, uow: UnitOfWork) -> Credits:
    items = list_items(ctx, uow, CreditIn(kind="unapplied_credit")).items
    by_currency: dict[str, list[OpenItemRow]] = defaultdict(list)
    for row in items:
        if row.currency is not None:
            by_currency[row.currency].append(row)
    sums = [
        CreditSum(
            currency=currency,
            amount=sum((abs(r.amount) for r in rows if r.amount is not None), ZERO),
            cad_amount=None
            if any(r.cad_amount is None for r in rows)
            else sum(
                (abs(r.cad_amount) for r in rows if r.cad_amount is not None), ZERO
            ),
        )
        for currency, rows in sorted(by_currency.items())
    ]
    amounts = "; ".join(f"{row.amount:.2f} {row.currency}" for row in sums)
    line = f"credits not applied: {len(items)}" + (f", {amounts}" if amounts else "")
    return Credits(count=len(items), items=items, sums=sums, line=line)


class ListOut(BaseModel):
    model_config = ConfigDict(frozen=True)
    as_of: Annotated[str, Tiered(P)]
    today: Annotated[dt.date, Tiered(P)]
    window_days: Annotated[int, Tiered(P)]
    currency: Annotated[str, Tiered(P)]
    due_in_window: Annotated[Decimal, Tiered(R)]
    overdue: Annotated[Decimal, Tiered(R)]
    carried: Annotated[Decimal, Tiered(R)]
    rows: Annotated[list[OwedRow], Tiered(P)]
    more: Annotated[int, Tiered(P)]
    credits: Annotated[Credits, Tiered(P)]
    gaps: Annotated[list[Gap], Tiered(P)]
    gap_count: Annotated[int, Tiered(P)]
    headline: Annotated[list[str], Tiered(R)]


def list_owed(ctx: FinanceContext, uow: UnitOfWork, _data: ListIn) -> ListOut:
    today = local_date(ctx.clock.now_utc(), ctx.settings.timezone)
    window = ctx.settings.get_int("finance.owed.window_days")
    cap = ctx.settings.get_int("finance.owed.max_rows")
    rows = [
        figures(Facts.model_validate(dict(row)), today, window)
        for row in uow.execute(select(report_owed)).mappings()
    ]
    rows.sort(
        key=lambda r: (
            r.overdue <= 0,
            r.due_date or dt.date.max,
            r.payee_slug,
            r.period_label,
            r.obligation_ref,
        )
    )
    due = sum((r.due_in_window for r in rows), ZERO)
    overdue = sum((r.overdue for r in rows), ZERO)
    carried = sum((r.carried for r in rows), ZERO)
    missing = gaps(uow)
    stamp = as_of(ctx.clock, ctx.settings.timezone)
    return ListOut(
        as_of=stamp,
        today=today,
        window_days=window,
        currency="CAD",
        due_in_window=due,
        overdue=overdue,
        carried=carried,
        rows=rows[:cap],
        more=max(0, len(rows) - cap),
        credits=credits(ctx, uow),
        gaps=missing,
        gap_count=len(missing),
        headline=[
            stamp,
            f"due in the next {window} days: {due:.2f} CAD",
            f"overdue: {overdue:.2f} CAD",
            f"carried: {carried:.2f} CAD",
            f"bill gaps: {len(missing)}",
        ],
    )


class TraceIn(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    obligation_ref: str | None = None


class Evidence(BaseModel):
    model_config = ConfigDict(frozen=True)
    evidence_ref: Annotated[str, Tiered(P)]
    role: Annotated[str, Tiered(P)]
    txn_ref: Annotated[str | None, Tiered(P)]
    document_path: Annotated[str | None, Tiered(P)]
    line_no: Annotated[int | None, Tiered(P)]
    drop_ref: Annotated[str | None, Tiered(P)]


class Application(BaseModel):
    model_config = ConfigDict(frozen=True)
    application_ref: Annotated[str, Tiered(P)]
    payment_ref: Annotated[str, Tiered(P)]
    applied_amount: Annotated[Decimal, Tiered(R)]
    payment_amount: Annotated[Decimal, Tiered(R)]
    currency: Annotated[str, Tiered(P)]
    paid_on: Annotated[dt.date, Tiered(P)]
    source: Annotated[str, Tiered(P)]
    state: Annotated[str, Tiered(P)]
    basis_ref: Annotated[str | None, Tiered(P)]
    auto: Annotated[bool, Tiered(P)]
    evidence: Annotated[list[Evidence], Tiered(P)]


class TraceObligation(BaseModel):
    model_config = ConfigDict(frozen=True)
    row: Annotated[OwedRow, Tiered(P)]
    basis_class: Annotated[str | None, Tiered(P)]
    presumed_basis_kind: Annotated[str | None, Tiered(P)]
    presumed_at: Annotated[dt.datetime | None, Tiered(P)]
    applications: Annotated[list[Application], Tiered(P)]


class TraceOut(BaseModel):
    model_config = ConfigDict(frozen=True)
    obligations: Annotated[list[TraceObligation], Tiered(P)]
    credits: Annotated[Credits, Tiered(P)]


def trace(ctx: FinanceContext, uow: UnitOfWork, data: TraceIn) -> TraceOut:
    today = local_date(ctx.clock.now_utc(), ctx.settings.timezone)
    window = ctx.settings.get_int("finance.owed.window_days")
    # Read all history to follow both directions of a supersession chain. The row cap
    # affects the list only, never trace completeness.
    history: dict[str, TraceObligation] = {}
    applications: dict[str, dict[str, Application]] = defaultdict(dict)
    for r in uow.execute(
        select(report_owed_trace).order_by(
            report_owed_trace.c.obligation_ref,
            report_owed_trace.c.paid_on,
            report_owed_trace.c.application_ref,
            report_owed_trace.c.evidence_ref,
        )
    ).mappings():
        ref = r.obligation_ref
        if ref not in history:
            history[ref] = TraceObligation(
                row=figures(Facts.model_validate(dict(r)), today, window),
                basis_class="owner" if r.presumed_paid_basis else None,
                presumed_basis_kind=r.presumed_basis_kind,
                presumed_at=r.presumed_at,
                applications=[],
            )
        if r.application_ref is None:
            continue
        apps = applications[ref]
        if r.application_ref not in apps:
            apps[r.application_ref] = Application(
                application_ref=r.application_ref,
                payment_ref=r.payment_ref,
                applied_amount=r.applied_amount,
                payment_amount=r.payment_amount,
                currency=r.currency,
                paid_on=r.paid_on,
                source=r.payment_source,
                state=r.payment_state,
                basis_ref=r.application_basis,
                auto=r.application_auto,
                evidence=[],
            )
        if r.evidence_ref is not None:
            apps[r.application_ref].evidence.append(
                Evidence(
                    evidence_ref=r.evidence_ref,
                    role=r.evidence_role,
                    txn_ref=r.txn_ref,
                    document_path=r.evidence_document,
                    line_no=r.line_no,
                    drop_ref=r.drop_ref,
                )
            )
    selected = (
        set(history)
        if data.obligation_ref is None
        else {data.obligation_ref} & history.keys()
    )
    while True:
        connected = {
            ref
            for ref, item in history.items()
            if item.row.supersedes_ref in selected
            or ref in {history[key].row.supersedes_ref for key in selected}
        }
        expanded = selected | connected
        if expanded == selected:
            break
        selected = expanded
    result = [
        history[ref].model_copy(
            update={"applications": list(applications[ref].values())}
        )
        for ref in sorted(selected)
    ]
    return TraceOut(obligations=result, credits=credits(ctx, uow))


register(
    OperationSpec(
        name="finance.owed.list",
        input_model=ListIn,
        output_model=ListOut,
        handler=list_owed,
        tool_name="finance_owed",
        description="Open obligations, read-time figures, credits and bill gaps.",
        safety_class=SafetyClass.READ,
        roles=frozenset(Role),
        connection="reader",
    )
)
register(
    OperationSpec(
        name="finance.owed.trace",
        input_model=TraceIn,
        output_model=TraceOut,
        handler=trace,
        tool_name="finance_trace",
        description="Obligation source chains, applications, notices and credits.",
        safety_class=SafetyClass.READ,
        roles=frozenset(Role),
        connection="reader",
    )
)
