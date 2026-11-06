"""Owed command rendering over the reader operations, dispatched as the owner."""

from __future__ import annotations

import argparse
from typing import TYPE_CHECKING

from rheo_finance import db
from rheo_finance.render import Report, emit, emit_error, emit_json, money

if TYPE_CHECKING:
    from rheo_finance.reports.owed import Credits, Gap, OwedRow, TraceOut


def _source(row: OwedRow) -> str:
    if not row.primary_source.strip():
        raise ValueError("owed row has no source reference")
    return row.primary_source + (
        f"; manual {row.manual_source}" if row.manual_source else ""
    )


def _row(row: OwedRow) -> list[str]:
    if row.kind == "card_statement":
        amounts = (
            f"balance {money(row.amount_due, 'CAD')}; "
            + (
                f"minimum {money(row.minimum_due, 'CAD')}"
                if row.minimum_due is not None
                else "minimum not printed "
                f"(threshold {money(row.threshold_due, 'CAD')})"
            )
            + f"; paid {money(row.paid, 'CAD')}; overdue {money(row.overdue, 'CAD')}; "
            f"carried {money(row.carried, 'CAD')}; "
            f"remaining {money(row.remaining, 'CAD')}"
        )
    else:
        amounts = f"remaining {money(row.remaining, 'CAD')}"
    days = row.days_until_due
    timing = (
        "no due date"
        if days is None
        else f"due {row.due_date}; "
        + (f"{abs(days)} days overdue" if days < 0 else f"{days} days remaining")
    )
    lines = [
        f"{row.obligation_ref} {row.payee_label} ({row.period_label}): {amounts}; "
        f"{timing}; source {_source(row)}"
        + ("; before reminders floor" if row.before_floor else "")
    ]
    if row.payments_unmatchable:
        lines.append(
            f"  {row.invoice_folder or row.payee_slug}: payments cannot be matched "
            "until this vendor is in payees.csv"
        )
    for label, amount in [
        ("manual_mismatch", row.manual_mismatch),
        ("payments_mismatch", row.payments_mismatch),
    ]:
        if amount is not None:
            lines.append(f"  {label}: {money(amount, 'CAD')}")
    if row.statement_conflict:
        lines.append("  statement conflict")
    return lines


def _gap(row: Gap) -> str:
    period = (
        f"; period {row.period_start} to {row.period_end}"
        if row.period_start is not None and row.period_end is not None
        else f"; bill date {row.period_start or row.period_end}"
        if row.period_start is not None or row.period_end is not None
        else ""
    )
    return f"gap: {row.path}; {row.issuer_label}; {row.kind}; {row.status}{period}"


def _credits(out: Credits) -> list[str]:
    lines = [out.line]
    for row in out.items:
        amount = (
            money(abs(row.amount), row.currency)
            if row.amount is not None and row.currency is not None
            else "no stored amount"
        )
        lines.append(
            f"  credit {row.line_ref}; {row.payment_ref or 'no payment reference'}; "
            f"{row.payee_label or 'no payee'}; {row.occurred_on}; "
            f"{amount}; {row.reason}"
        )
    return lines


def _trace(out: TraceOut) -> Report:
    detail: list[str] = []
    for item in out.obligations:
        row = item.row
        detail.extend(_row(row))
        detail.append(
            f"  status {row.status}; recorded paid {money(row.recorded_paid, 'CAD')}; "
            f"recorded remaining {money(row.recorded_remaining, 'CAD')}; "
            "recorded threshold remaining "
            f"{money(row.recorded_threshold_remaining, 'CAD')}"
        )
        if row.supersedes_ref:
            detail.append(f"  supersedes {row.supersedes_ref}")
        if row.presumed_paid_basis:
            detail.append(
                f"  presumed-paid basis {row.presumed_paid_basis}; "
                f"{item.basis_class}; {item.presumed_basis_kind}; {item.presumed_at}"
            )
        if row.payments_unmatchable:
            detail.append(
                "  no payment can reach this obligation until vendor registration"
            )
        for app in item.applications:
            detail.append(
                f"  application {app.application_ref}; payment {app.payment_ref}; "
                f"{app.paid_on}; applied {money(app.applied_amount, app.currency)}; "
                f"payment amount {money(app.payment_amount, app.currency)}; "
                f"{app.source}; {app.state}; auto {app.auto}"
                + (f"; basis {app.basis_ref}" if app.basis_ref else "")
            )
            for evidence in app.evidence:
                detail.append(
                    f"    evidence {evidence.evidence_ref}; {evidence.role}"
                    + (f"; txn {evidence.txn_ref}" if evidence.txn_ref else "")
                    + (
                        f"; source {evidence.document_path}"
                        if evidence.document_path
                        else ""
                    )
                    + (
                        f"; line {evidence.line_no}"
                        if evidence.line_no is not None
                        else ""
                    )
                    + (f"; drop {evidence.drop_ref}" if evidence.drop_ref else "")
                )
    detail.extend(_credits(out.credits))
    return Report([f"finance owed trace: {len(out.obligations)} obligations"], detail)


def _cmd_owed(args: argparse.Namespace) -> int:
    from rheo_finance import composition, operations
    from rheo_finance.contracts import Role
    from rheo_finance.reports.owed import ListOut, TraceOut

    ctx = composition.build_context(Role.OWNER)
    try:
        if args.trace is not None:
            out = operations.dispatch(
                "finance.owed.trace",
                {"obligation_ref": args.trace},
                Role.OWNER,
                ctx=ctx,
            )
            assert isinstance(out, TraceOut)
            for item in out.obligations:
                _source(item.row)
            if args.json:
                emit_json(out)
            else:
                emit(_trace(out))
        else:
            out = operations.dispatch("finance.owed.list", {}, Role.OWNER, ctx=ctx)
            assert isinstance(out, ListOut)
            for row in out.rows:
                _source(row)
            if args.gaps:
                if args.json:
                    emit_json({"gaps": out.gaps, "gap_count": out.gap_count})
                else:
                    emit(
                        Report(
                            [f"bill gaps: {out.gap_count}"], [_gap(g) for g in out.gaps]
                        )
                    )
            elif args.json:
                emit_json(out)
            else:
                detail = [line for row in out.rows for line in _row(row)]
                if not out.rows:
                    detail.append("no open obligations")
                if out.more:
                    detail.append(f"+{out.more} more")
                detail.append(out.credits.line)
                detail.extend(_gap(g) for g in out.gaps)
                emit(Report(out.headline, detail))
    except operations.OperationError as exc:
        emit_error(Report([f"finance owed: refused. {exc}"]))
        return 1
    except Exception as exc:
        if not db.unreachable(exc):
            raise
        emit_error(Report([f"finance owed: {db.UNREACHABLE}"]))
        return 1
    return 0


def add_parser(commands: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = commands.add_parser(
        "owed", help="open obligations and sources (reader connection)"
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--trace", metavar="ROW", help="trace an obligation and its payments"
    )
    mode.add_argument("--gaps", action="store_true", help="show only bill gaps")
    parser.add_argument("--json", action="store_true", help="print JSON")
    parser.set_defaults(handler=_cmd_owed)
