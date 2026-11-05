"""Stage 17's orphan lifecycle (FR-18), from retained decision snapshots.

Snapshots only suggest replacements. A same-kind answer suppresses an orphan only
when its named replacements match those snapshots. Accountless payment notices
have no snapshot, so their missing keys remain listed without invented candidates.
Merchant category answers use the same snapshot matching, while their current
presence and coverage are scoped by merchant key. Split decisions join later.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections import defaultdict
from dataclasses import dataclass
from decimal import ROUND_HALF_EVEN, Decimal

from sqlalchemy import insert, select, update

from rheo_finance.context import UnitOfWork
from rheo_finance.decision import Decision, reversed_clause
from rheo_finance.decision import DecisionKind as K
from rheo_finance.derive.stage15 import NOTICE_KIND
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
RefKey = tuple[str, uuid.UUID]
Bucket = tuple[str, str, Decimal]


def _line_key(ref: str | None) -> RefKey | None:
    if ref is None:
        return None
    parsed = refs.parse(ref)
    if parsed.system == refs.FINANCE_SYSTEM and parsed.type in (
        refs.LINE_TYPE,
        NOTICE_KIND,
    ):
        return parsed.type, uuid.UUID(parsed.id)
    return None


def _obligation_key(ref: str | None) -> uuid.UUID | None:
    if ref is None:
        return None
    parsed = refs.parse(ref)
    if parsed.system == refs.FINANCE_SYSTEM and parsed.type == "obligation":
        return uuid.UUID(parsed.id)
    return None


@dataclass(frozen=True)
class _Snapshot:
    key: RefKey
    bucket: Bucket
    day: dt.date


def _snapshots(
    uow: UnitOfWork,
    wanted: set[uuid.UUID],
) -> dict[uuid.UUID, list[_Snapshot]]:
    """Immutable candidate facts, including a replacement whose key is now gone."""
    snapshots: dict[uuid.UUID, list[_Snapshot]] = defaultdict(list)
    for row in uow.execute(
        select(
            decision_target.c.decision_id,
            decision_target.c.target_ref,
            decision_target.c.account_slug,
            decision_target.c.currency,
            decision_target.c.amount,
            decision_target.c.posting_date,
        )
        .where(decision_target.c.decision_id.in_(wanted))
        .order_by(decision_target.c.decision_id, decision_target.c.seq)
    ):
        key = _line_key(row.target_ref)
        if key is None:
            continue
        bucket = (
            row.account_slug,
            row.currency,
            row.amount.quantize(CENT, rounding=ROUND_HALF_EVEN),
        )
        snapshots[row.decision_id].append(_Snapshot(key, bucket, row.posting_date))
    return snapshots


def _matches(
    uow: UnitOfWork,
    snapshots: dict[uuid.UUID, list[_Snapshot]],
    overlap_days: int,
) -> dict[uuid.UUID, list[set[uuid.UUID]]]:
    """Bucket current candidates once before filtering dates; no snapshots, no read."""
    if not snapshots:
        return {}
    wanted = {snapshot.bucket for found in snapshots.values() for snapshot in found}
    buckets: dict[Bucket, list[tuple[uuid.UUID, dt.date]]] = defaultdict(list)
    current = select(txn_source.c.txn_id).where(
        txn_source.c.role.in_(("primary", "additional"))
    )
    for line in uow.execute(
        select(
            txn.c.id,
            account.c.slug,
            txn.c.currency,
            txn.c.amount,
            txn.c.posting_date,
        )
        .join(account, account.c.id == txn.c.account_id)
        .where(
            account.c.slug.in_({key[0] for key in wanted}),
            txn.c.currency.in_({key[1] for key in wanted}),
            txn.c.id.in_(current),
        )
    ):
        bucket = (
            line.slug,
            line.currency,
            line.amount.quantize(CENT, rounding=ROUND_HALF_EVEN),
        )
        if bucket in wanted:
            buckets[bucket].append((line.id, line.posting_date))
    return {
        key: [
            {
                line_id
                for line_id, day in buckets[snapshot.bucket]
                if abs((day - snapshot.day).days) <= overlap_days
            }
            for snapshot in found
        ]
        for key, found in snapshots.items()
    }


def _categories(
    uow: UnitOfWork,
    rows: list[Decision],
    resolved: dict[RefKey, uuid.UUID],
    merchants: dict[uuid.UUID, str],
    inactive: set[uuid.UUID],
) -> None:
    """Apply owner fields only, line answers before their merchant's answers."""
    units: dict[str, set[uuid.UUID]] = defaultdict(set)
    for line_id, merchant in merchants.items():
        units[merchant].add(line_id)
    categories: set[uuid.UUID] = set()
    tags: set[uuid.UUID] = set()
    # Rows are already newest first. A line's older answer still outranks a newer
    # unit answer, independently for category and tag (FR-17).
    answers = [row for row in rows if row.kind is K.CATEGORY and row.id not in inactive]
    answers.sort(key=lambda row: row.merchant_key is not None)
    for row in answers:
        if row.merchant_key is not None:
            targets = units[row.merchant_key]
        else:
            key = _line_key(row.target_ref)
            target = resolved.get(key) if key is not None else None
            targets = {target} if target is not None else set()
        basis = refs.format_ref(refs.FINANCE_SYSTEM, "decision", row.id)
        for target in targets:
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


def _covers(options: list[set[uuid.UUID]], replacements: set[uuid.UUID]) -> bool:
    """One distinct replacement per original snapshot, regardless of pair order."""
    if len(options) != len(replacements):
        return False
    owners: dict[uuid.UUID, int] = {}

    def assign(index: int, seen: set[uuid.UUID]) -> bool:
        for candidate in options[index] & replacements:
            if candidate in seen:
                continue
            seen.add(candidate)
            if candidate not in owners or assign(owners[candidate], seen):
                owners[candidate] = index
                return True
        return False

    return all(assign(index, set()) for index in range(len(options)))


def build(uow: UnitOfWork, *, overlap_days: int) -> None:
    """List orphans and mark candidates without interpreting arbitrary about uses
    as successful reattachments. Called once after obligations exist."""
    rows = [
        Decision.model_validate(dict(row))
        for row in uow.execute(
            select(decision)
            .where(
                decision.c.kind.in_(
                    [
                        kind.value
                        for kind in (
                            LINE_KINDS | OBLIGATION_KINDS | {K.REATTACH_DECLINE}
                        )
                    ]
                ),
                ~reversed_clause(decision),
            )
            .order_by(decision.c.decided_at.desc(), decision.c.id.desc())
        ).mappings()
    ]
    if not any(row.kind in LINE_KINDS | OBLIGATION_KINDS for row in rows):
        return
    legs: dict[uuid.UUID, list[str]] = defaultdict(list)
    paired = {row.id for row in rows if row.kind in (K.PAIR, K.UNPAIR)}
    if paired:
        for leg in uow.execute(
            select(decision_item)
            .where(
                decision_item.c.decision_id.in_(paired), decision_item.c.role == "leg"
            )
            .order_by(decision_item.c.seq)
        ):
            legs[leg.decision_id].append(leg.target_ref)
    snapshots = _snapshots(uow, {row.id for row in rows})
    merchants = (
        {
            row.id: row.merchant_key
            for row in uow.execute(select(txn.c.id, txn.c.merchant_key))
            if row.merchant_key is not None
        }
        if any(row.merchant_key is not None for row in rows)
        else {}
    )
    merchant_owners: dict[str, set[uuid.UUID]] = defaultdict(set)
    for row in rows:
        if row.kind is K.CATEGORY and row.merchant_key is not None:
            merchant_owners[row.merchant_key].add(row.id)
    named = {
        row.id: [
            key
            for ref in (
                row.target_ref,
                row.counterpart_ref,
                *legs[row.id],
            )
            if (key := _line_key(ref)) is not None
        ]
        for row in rows
    }
    for row in rows:
        if row.merchant_key is not None:
            named[row.id] = [snapshot.key for snapshot in snapshots.get(row.id, [])]
    wanted = {key for found in named.values() for key in found}
    resolved: dict[RefKey, uuid.UUID] = {}
    present: set[RefKey] = set()
    if wanted:
        txn_keys = {key for kind, key in wanted if kind == refs.LINE_TYPE}
        if txn_keys:
            resolved = {
                (refs.LINE_TYPE, row.line_key_id): row.txn_id
                for row in uow.execute(
                    select(txn_source.c.line_key_id, txn_source.c.txn_id).where(
                        txn_source.c.line_key_id.in_(txn_keys),
                        txn_source.c.role.in_(("primary", "additional")),
                    )
                )
            }
        present.update(resolved)
        for line in uow.execute(
            select(document_line.c.line_key_id, document_line.c.line_kind).where(
                document_line.c.line_key_id.in_({key for _, key in wanted})
            )
        ):
            present.add((refs.LINE_TYPE, line.line_key_id))
            if line.line_kind == NOTICE_KIND:
                present.add((NOTICE_KIND, line.line_key_id))
    obligation_keys = {
        row.id: _obligation_key(row.obligation_ref)
        for row in rows
        if row.kind in OBLIGATION_KINDS
    }
    known_obligations = (
        set(
            uow.scalars(
                select(obligation.c.id).where(
                    obligation.c.id.in_(
                        {key for key in obligation_keys.values() if key is not None}
                    )
                )
            )
        )
        if obligation_keys
        else set()
    )
    current_merchants = set(merchants.values())
    orphaned = {
        row.id
        for row in rows
        if (
            row.kind in LINE_KINDS
            and row.merchant_key is None
            and any(key not in present for key in named[row.id])
        )
        or (row.merchant_key is not None and row.merchant_key not in current_merchants)
        or (
            row.id in obligation_keys
            and obligation_keys[row.id] is not None
            and obligation_keys[row.id] not in known_obligations
        )
    }
    if not orphaned:
        _categories(uow, rows, resolved, merchants, set())
        return

    by_id = {row.id: row for row in rows}
    taken: dict[tuple[K, uuid.UUID], set[uuid.UUID]] = defaultdict(set)
    declined: dict[uuid.UUID, set[uuid.UUID]] = defaultdict(set)
    for row in rows:
        if row.merchant_key is not None:
            for target, merchant in merchants.items():
                if merchant == row.merchant_key:
                    taken[row.kind, target].add(row.id)
        for key in named[row.id]:
            if key in resolved and row.merchant_key is None:
                taken[row.kind, resolved[key]].add(row.id)
                if row.kind is K.REATTACH_DECLINE and row.about_decision_id is not None:
                    declined[row.about_decision_id].add(resolved[key])
    matching = _matches(
        uow,
        {key: found for key, found in snapshots.items() if key in orphaned},
        overlap_days,
    )
    recorded: dict[tuple[K, RefKey], set[uuid.UUID]] = defaultdict(set)
    declines: dict[uuid.UUID, list[Decision]] = defaultdict(list)
    for row in rows:
        if row.merchant_key is None:
            for key in named[row.id]:
                recorded[row.kind, key].add(row.id)
        if row.kind is K.REATTACH_DECLINE and row.about_decision_id is not None:
            declines[row.about_decision_id].append(row)
    reattached: set[uuid.UUID] = set()
    for answer in rows:
        about = answer.about_decision_id
        original = by_id.get(about) if about is not None else None
        if (
            original is None
            or original.id not in orphaned
            or answer.kind != original.kind
            or (answer.merchant_key is None) != (original.merchant_key is None)
            or (answer.decided_at, answer.id) <= (original.decided_at, original.id)
        ):
            continue
        replacements = snapshots.get(answer.id, [])
        if len(replacements) != len(named[answer.id]) or len(
            snapshots.get(original.id, [])
        ) != len(named[original.id]):
            continue
        matches = [
            {
                replacement.key[1]
                for replacement in replacements
                if replacement.bucket == snapshot.bucket
                and abs((replacement.day - snapshot.day).days) <= overlap_days
            }
            for snapshot in snapshots.get(original.id, [])
        ]
        if not matches or not _covers(matches, {row.key[1] for row in replacements}):
            continue
        # The answer itself must not disqualify its candidate. Later answers may
        # change the classification without reviving this already answered orphan.
        blocked = False
        for replacement in replacements:
            current_id = resolved.get(replacement.key)
            owners = recorded[answer.kind, replacement.key] | (
                taken[answer.kind, current_id] if current_id is not None else set()
            )
            if answer.merchant_key is not None:
                owners |= merchant_owners[answer.merchant_key]
                if (
                    current_id is not None
                    and merchants.get(current_id) != answer.merchant_key
                ):
                    blocked = True
            if any(
                other not in (original.id, answer.id)
                and (by_id[other].decided_at, other) < (answer.decided_at, answer.id)
                for other in owners
            ):
                blocked = True
            for decline in declines[original.id]:
                if (decline.decided_at, decline.id) >= (answer.decided_at, answer.id):
                    continue
                if replacement.key in named[decline.id] or (
                    current_id is not None
                    and any(
                        resolved.get(key) == current_id for key in named[decline.id]
                    )
                ):
                    blocked = True
        if blocked:
            continue
        reattached.add(original.id)
    orphaned -= reattached
    candidates = {
        key: {
            candidate
            for options in matching.get(key, [])
            for candidate in options
            if not (taken[by_id[key].kind, candidate] - {key} - reattached)
            and candidate not in declined[key]
        }
        for key in orphaned
    }
    values = [
        {
            "decision_id": key,
            "candidate_txn_id": candidate,
            "candidate_merchant_key": merchants.get(candidate)
            if by_id[key].merchant_key is not None
            else None,
        }
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
    _categories(uow, rows, resolved, merchants, orphaned | reattached)
