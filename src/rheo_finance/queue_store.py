"""Storage reads for the computed confirm queue. No queue rows are persisted."""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass
from decimal import Decimal

from pydantic import BaseModel
from sqlalchemy import select

from rheo_finance.context import UnitOfWork
from rheo_finance.decision import Decision, LegIn, reversed_clause
from rheo_finance.storage import schema as s


class Line(BaseModel):
    id: uuid.UUID
    account_slug: str
    posting_date: dt.date
    amount: Decimal
    currency: str
    cad_amount: Decimal | None
    merchant_key: str | None
    display_label: str | None
    category: str | None
    category_basis_class: str | None
    orphan_candidate: bool
    exclusion: str | None


class Payment(BaseModel):
    id: uuid.UUID
    payee_id: uuid.UUID
    payee_label: str
    amount: Decimal
    currency: str
    paid_on: dt.date
    source: str
    state: str


class Obligation(BaseModel):
    id: uuid.UUID
    payee_id: uuid.UUID
    payee_label: str
    kind: str
    period_label: str
    statement_date: dt.date | None
    issued_on: dt.date | None
    due_date: dt.date | None
    amount_due: Decimal
    remaining: Decimal
    status: str


class Evidence(BaseModel):
    payment_id: uuid.UUID
    role: str
    txn_id: uuid.UUID | None
    line_key_id: uuid.UUID | None
    posting_date: dt.date | None
    amount: Decimal | None
    currency: str | None


class Snapshot(BaseModel):
    decision_id: uuid.UUID
    target_ref: str
    account_slug: str
    currency: str
    amount: Decimal
    posting_date: dt.date


@dataclass(frozen=True)
class Facts:
    lines: dict[uuid.UUID, Line]
    keys: dict[uuid.UUID, uuid.UUID]
    aliases: dict[uuid.UUID, uuid.UUID]
    edges: list[tuple[uuid.UUID, str, uuid.UUID, uuid.UUID]]
    payments: list[Payment]
    evidence: list[Evidence]
    confirm_payments: set[uuid.UUID]
    obligations: dict[uuid.UUID, Obligation]
    decisions: list[Decision]
    orphans: dict[uuid.UUID, set[uuid.UUID]]
    snapshots: list[Snapshot]
    legs: dict[uuid.UUID, list[LegIn]]


def read(uow: UnitOfWork) -> Facts:
    lines = {
        line.id: line
        for raw in uow.execute(
            select(
                *(
                    s.txn.c[name]
                    for name in Line.model_fields
                    if name != "account_slug"
                ),
                s.account.c.slug.label("account_slug"),
            ).join(s.account, s.account.c.id == s.txn.c.account_id)
        ).mappings()
        if (line := Line.model_validate(dict(raw)))
    }
    # Primary first, then the key itself: aliases never move the target of an answer.
    keys: dict[uuid.UUID, uuid.UUID] = {}
    aliases: dict[uuid.UUID, uuid.UUID] = {}
    for row in uow.execute(
        select(s.txn_source.c.txn_id, s.txn_source.c.line_key_id)
        .where(s.txn_source.c.role.in_(("primary", "additional")))
        .order_by((s.txn_source.c.role != "primary"), s.txn_source.c.line_key_id)
    ):
        keys.setdefault(row.txn_id, row.line_key_id)
        aliases[row.line_key_id] = row.txn_id
    edges = [
        tuple(row)
        for row in uow.execute(
            select(
                s.pair_edge.c.id,
                s.pair_edge.c.kind,
                s.pair_edge.c.a_txn_id,
                s.pair_edge.c.b_txn_id,
            )
            .where(
                ~s.pair_edge.c.applied,
                s.pair_edge.c.a_txn_id.is_not(None),
                s.pair_edge.c.b_txn_id.is_not(None),
            )
            .order_by(s.pair_edge.c.id)
        )
    ]
    payments = [
        Payment.model_validate(dict(row))
        for row in uow.execute(
            select(s.payment, s.payee.c.display_label.label("payee_label"))
            .join(s.payee, s.payee.c.id == s.payment.c.payee_id)
            .order_by(s.payment.c.id)
        ).mappings()
    ]
    evidence = [
        Evidence.model_validate(dict(row))
        for row in uow.execute(
            select(
                s.payment_evidence.c.payment_id,
                s.payment_evidence.c.role,
                s.payment_evidence.c.txn_id,
                s.document_line.c.line_key_id,
                s.document_line.c.posting_date,
                s.document_line.c.amount,
                s.document_line.c.currency,
            )
            .outerjoin(
                s.document_line,
                s.document_line.c.id == s.payment_evidence.c.document_line_id,
            )
            .order_by(s.payment_evidence.c.payment_id, s.payment_evidence.c.id)
        ).mappings()
    ]
    obligations = {
        owed.id: owed
        for raw in uow.execute(
            select(s.obligation, s.payee.c.display_label.label("payee_label"))
            .join(s.payee, s.payee.c.id == s.obligation.c.payee_id)
            .order_by(s.obligation.c.id)
        ).mappings()
        if (owed := Obligation.model_validate(dict(raw)))
    }
    decisions = [
        Decision.model_validate(dict(row))
        for row in uow.execute(
            select(s.decision)
            .where(~reversed_clause(s.decision))
            .order_by(s.decision.c.decided_at.desc(), s.decision.c.id.desc())
        ).mappings()
    ]
    orphans: dict[uuid.UUID, set[uuid.UUID]] = {}
    for row in uow.execute(select(s.decision_orphan)):
        if row.candidate_txn_id is not None:
            orphans.setdefault(row.decision_id, set()).add(row.candidate_txn_id)
    snapshots = [
        Snapshot.model_validate(dict(row))
        for row in uow.execute(
            select(s.decision_target)
            .where(s.decision_target.c.decision_id.in_(orphans))
            .order_by(s.decision_target.c.decision_id, s.decision_target.c.seq)
        ).mappings()
    ]
    legs: dict[uuid.UUID, list[LegIn]] = {}
    for row in uow.execute(
        select(s.decision_item)
        .where(
            s.decision_item.c.decision_id.in_(orphans), s.decision_item.c.role == "leg"
        )
        .order_by(s.decision_item.c.decision_id, s.decision_item.c.seq)
    ):
        legs.setdefault(row.decision_id, []).append(
            LegIn(target_ref=row.target_ref, amount=row.amount)
        )
    return Facts(
        lines,
        keys,
        aliases,
        edges,
        payments,
        evidence,
        set(
            uow.scalars(
                select(s.payment_application.c.payment_id).where(
                    s.payment_application.c.reason == "confirm"
                )
            )
        ),
        obligations,
        decisions,
        orphans,
        snapshots,
        legs,
    )
