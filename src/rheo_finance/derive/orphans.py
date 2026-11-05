"""Stage 17's orphan lifecycle (FR-18), from retained decision snapshots.

Snapshots only suggest replacements. Only another decision of the same kind can
reattach an orphan; a decline hides just its named candidate until reversed.
Merchant keys and split decisions belong to their later stages.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from decimal import ROUND_HALF_EVEN, Decimal

from sqlalchemy import insert, select, update

from rheo_finance.context import UnitOfWork
from rheo_finance.decision import Decision, reversed_clause
from rheo_finance.decision import DecisionKind as K
from rheo_finance.storage import refs
from rheo_finance.storage.schema import (
    account,
    decision,
    decision_item,
    decision_orphan,
    decision_target,
    document_line,
    obligation,
    txn,
    txn_source,
)

LINE_KINDS = frozenset(
    {K.CATEGORY, K.PAIR, K.UNPAIR, K.INCLUDE, K.PAYMENT_APPLY, K.PAYMENT_UNAPPLY}
)
OBLIGATION_KINDS = frozenset({K.PRESUMED_PAID, K.OBLIGATION_DISPUTE})
LABEL = "decision orphaned"
CENT = Decimal("0.01")


def build(uow: UnitOfWork, *, overlap_days: int) -> None:
    """List orphans, mark their current candidates, and restore explicit line
    categories with their decision basis. Called once after obligations exist."""
    rows = [
        Decision.model_validate(dict(row))
        for row in uow.execute(
            select(decision)
            .where(~reversed_clause(decision))
            .order_by(decision.c.decided_at.desc(), decision.c.id.desc())
        ).mappings()
    ]
    legs: dict[uuid.UUID, list[str]] = defaultdict(list)
    for leg in uow.execute(select(decision_item).where(decision_item.c.role == "leg")):
        legs[leg.decision_id].append(leg.target_ref)
    resolved = {
        row.line_key_id: row.txn_id
        for row in uow.execute(
            select(txn_source.c.line_key_id, txn_source.c.txn_id).where(
                txn_source.c.role.in_(("primary", "additional"))
            )
        )
    }
    current = set(resolved) | set(uow.scalars(select(document_line.c.line_key_id)))
    obligations = set(uow.scalars(select(obligation.c.id)))

    def line_id(ref: str) -> uuid.UUID | None:
        parsed = refs.parse(ref)
        if parsed.system == refs.FINANCE_SYSTEM and parsed.type == refs.LINE_TYPE:
            return uuid.UUID(parsed.id)
        return None

    def named(row: Decision) -> list[uuid.UUID]:
        return [
            key
            for ref in (row.target_ref, row.counterpart_ref, *legs[row.id])
            if ref is not None and (key := line_id(ref)) is not None
        ]

    by_id = {row.id: row for row in rows}
    reattached = {
        row.about_decision_id
        for row in rows
        if row.about_decision_id in by_id
        and row.kind == by_id[row.about_decision_id].kind
        and (row.decided_at, row.id)
        > (by_id[row.about_decision_id].decided_at, row.about_decision_id)
    }
    orphaned: set[uuid.UUID] = set()
    for row in rows:
        if row.id in reattached:
            continue
        if row.kind in LINE_KINDS and row.target_ref is not None:
            if any(key not in current for key in named(row)):
                orphaned.add(row.id)
        elif row.kind in OBLIGATION_KINDS and row.obligation_ref is not None:
            parsed = refs.parse(row.obligation_ref)
            if (
                parsed.system == refs.FINANCE_SYSTEM
                and parsed.type == "obligation"
                and uuid.UUID(parsed.id) not in obligations
            ):
                orphaned.add(row.id)

    # A candidate already targeted by another active decision of the same kind
    # must not inherit a second answer, even through an additional source key.
    taken: dict[tuple[K, uuid.UUID], set[uuid.UUID]] = defaultdict(set)
    for row in rows:
        if row.id not in reattached:
            for key in named(row):
                if key in resolved:
                    taken[row.kind, resolved[key]].add(row.id)
    declined: dict[uuid.UUID, set[uuid.UUID]] = defaultdict(set)
    for row in rows:
        if row.kind is K.REATTACH_DECLINE and row.about_decision_id is not None:
            declined[row.about_decision_id].update(
                resolved[key] for key in named(row) if key in resolved
            )
    lines = uow.execute(
        select(txn, account.c.slug)
        .join(account, account.c.id == txn.c.account_id)
        .where(txn.c.id.in_(set(resolved.values())))
    ).all()
    # Keep SQL date values typed by the row below; no conversion to local time.
    candidates: dict[uuid.UUID, set[uuid.UUID]] = defaultdict(set)
    for snapshot in uow.execute(
        select(decision_target).where(decision_target.c.decision_id.in_(orphaned))
    ):
        for line in lines:
            if (
                line.slug == snapshot.account_slug
                and line.currency == snapshot.currency
                and line.amount.quantize(CENT, rounding=ROUND_HALF_EVEN)
                == snapshot.amount.quantize(CENT, rounding=ROUND_HALF_EVEN)
                and abs((line.posting_date - snapshot.posting_date).days)
                <= overlap_days
                and not (
                    taken[by_id[snapshot.decision_id].kind, line.id]
                    - {snapshot.decision_id}
                )
                and line.id not in declined[snapshot.decision_id]
            ):
                candidates[snapshot.decision_id].add(line.id)
    values = [
        {"decision_id": key, "candidate_txn_id": candidate}
        for key in orphaned
        for candidate in (tuple(candidates[key]) or (None,))
    ]
    if values:
        uow.execute(insert(decision_orphan), values)
    marked = {candidate for found in candidates.values() for candidate in found}
    if marked:
        uow.execute(
            update(txn).where(txn.c.id.in_(marked)).values(orphan_candidate=True)
        )

    # Minimal line-only application for the orphan reattachment contract. The
    # classification stage will own merchant/rule/proposal precedence later.
    categories: set[uuid.UUID] = set()
    tags: set[uuid.UUID] = set()
    for row in rows:
        if (
            row.kind is not K.CATEGORY
            or row.target_ref is None
            or row.id in orphaned
            or row.id in reattached
        ):
            continue
        target_key = line_id(row.target_ref)
        target = resolved.get(target_key) if target_key is not None else None
        if target is None:
            continue
        basis = refs.format_ref(refs.FINANCE_SYSTEM, "decision", row.id)
        changes: dict[str, object] = {}
        if row.category is not None and target not in categories:
            changes.update(
                category=row.category,
                category_basis_ref=basis,
                category_basis_class="owner",
            )
            categories.add(target)
        if row.tag is not None and target not in tags:
            changes.update(
                tag=row.tag,
                business_unit=row.business_unit,
                business_pct=row.business_pct,
                tag_basis_ref=basis,
                tag_basis_class="owner",
            )
            tags.add(target)
        if changes:
            uow.execute(update(txn).where(txn.c.id == target).values(**changes))
