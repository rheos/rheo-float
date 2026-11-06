"""Stored facts for owed readers. Date-sensitive figures belong in the operation."""

from __future__ import annotations

from sqlalchemy import Select, func, select

from rheo_finance.storage.schema import (
    decision,
    document,
    document_line,
    obligation,
    payee,
    payment,
    payment_application,
    payment_evidence,
)


def obligation_select() -> Select[tuple[object, ...]]:
    o = obligation.c
    return select(
        func.concat("finance.obligation:", o.id).label("obligation_ref"),
        payee.c.slug.label("payee_slug"),
        payee.c.display_label.label("payee_label"),
        o.kind,
        o.period_label,
        o.statement_date,
        o.issued_on,
        o.amount_due,
        o.minimum_due,
        o.threshold_due,
        o.paid,
        o.remaining,
        o.threshold_remaining,
        o.due_date,
        o.status,
        o.primary_source,
        o.manual_source,
        o.before_floor,
        (func.cardinality(payee.c.patterns) == 0).label("payments_unmatchable"),
        o.manual_mismatch,
        o.payments_mismatch,
        o.statement_conflict,
        o.invoice_ref,
        o.arrangement_ref,
        func.nullif(
            func.concat("finance.obligation:", o.supersedes_id), "finance.obligation:"
        ).label("supersedes_ref"),
        func.nullif(
            func.concat("finance.decision:", o.presumed_paid_decision_id),
            "finance.decision:",
        ).label("presumed_paid_basis"),
    ).select_from(obligation.join(payee, o.payee_id == payee.c.id))


def report_owed_select() -> Select[tuple[object, ...]]:
    return obligation_select().where(
        obligation.c.status.in_(["open", "partial", "presumed_paid", "disputed"])
    )


def report_owed_trace_select() -> Select[tuple[object, ...]]:
    # One row per evidence item. The operation groups by application identity before
    # exposing money, so multiple notices never become multiple applications.
    return (
        obligation_select()
        .add_columns(
            func.nullif(
                func.concat("finance.payment_application:", payment_application.c.id),
                "finance.payment_application:",
            ).label("application_ref"),
            func.nullif(
                func.concat("finance.payment:", payment.c.id), "finance.payment:"
            ).label("payment_ref"),
            payment_application.c.amount.label("applied_amount"),
            payment_application.c.basis_ref.label("application_basis"),
            payment_application.c.auto.label("application_auto"),
            payment.c.amount.label("payment_amount"),
            payment.c.currency,
            payment.c.paid_on,
            payment.c.source.label("payment_source"),
            payment.c.state.label("payment_state"),
            func.nullif(
                func.concat("finance.payment_evidence:", payment_evidence.c.id),
                "finance.payment_evidence:",
            ).label("evidence_ref"),
            payment_evidence.c.role.label("evidence_role"),
            func.nullif(
                func.concat("finance.txn:", payment_evidence.c.txn_id), "finance.txn:"
            ).label("txn_ref"),
            document.c.path.label("evidence_document"),
            document_line.c.line_no,
            func.nullif(
                func.concat(
                    "finance.drop_evidence:", payment_evidence.c.drop_evidence_id
                ),
                "finance.drop_evidence:",
            ).label("drop_ref"),
            decision.c.kind.label("presumed_basis_kind"),
            decision.c.decided_at.label("presumed_at"),
        )
        .outerjoin(
            payment_application, payment_application.c.obligation_id == obligation.c.id
        )
        .outerjoin(payment, payment.c.id == payment_application.c.payment_id)
        .outerjoin(payment_evidence, payment_evidence.c.payment_id == payment.c.id)
        .outerjoin(
            document_line, document_line.c.id == payment_evidence.c.document_line_id
        )
        .outerjoin(document, document.c.id == document_line.c.document_id)
        .outerjoin(decision, decision.c.id == obligation.c.presumed_paid_decision_id)
    )
