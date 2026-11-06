"""Safe line projections; correlated annotations never multiply monetary rows."""

from __future__ import annotations

from sqlalchemy import Select, Text, case, cast, func, literal, select

from rheo_finance.decision import reversed_clause
from rheo_finance.storage.schema import account, decision, queue_count, txn, txn_source


def report_txn_select() -> Select[tuple[object, ...]]:
    primary = txn_source.alias("primary_source")
    named = txn_source.alias("split_source")
    kept_split = (
        select(decision.c.id)
        .select_from(
            decision.join(
                named,
                decision.c.target_ref
                == func.concat("finance.txn:", named.c.line_key_id),
            )
        )
        .where(
            named.c.txn_id == txn.c.id,
            named.c.role.in_(("primary", "additional")),
            decision.c.kind == "split",
            ~reversed_clause(decision),
        )
        .correlate(txn)
        .exists()
    )
    return select(
        func.concat("finance.txn:", primary.c.line_key_id).label("line_ref"),
        account.c.slug.label("account"),
        account.c.label.label("account_label"),
        txn.c.posting_date,
        txn.c.txn_date,
        txn.c.occurred_at,
        txn.c.recognition_date,
        txn.c.amount,
        txn.c.currency,
        txn.c.cad_amount,
        txn.c.basis,
        txn.c.provisional,
        func.coalesce(txn.c.display_label, "Transaction").label("display_label"),
        txn.c.category,
        txn.c.category_basis_class,
        txn.c.tag,
        txn.c.business_unit,
        txn.c.business_pct,
        txn.c.tag_basis_class,
        txn.c.transfer_state,
        txn.c.pair_kind,
        txn.c.allocation_state,
        txn.c.exclusion,
        case(
            (txn.c.orphan_candidate, "decision orphaned"), else_=cast(None, Text)
        ).label("decision_state"),
        case(
            (
                (txn.c.pair_kind == "reversal") & kept_split,
                "split kept, on a reversed line",
            ),
            else_=cast(None, Text),
        ).label("split_state"),
    ).select_from(
        txn.join(account, account.c.id == txn.c.account_id).join(
            primary, (primary.c.txn_id == txn.c.id) & (primary.c.role == "primary")
        )
    )


def report_crypto_movement_select() -> Select[tuple[object, ...]]:
    # Native units are summed only within a currency and state, never across assets.
    return (
        select(
            txn.c.currency,
            txn.c.movement_kind,
            txn.c.transfer_state.label("state"),
            txn.c.pair_kind,
            func.count().label("count"),
            func.sum(txn.c.amount).label("amount"),
            case(
                (
                    func.count(txn.c.cad_amount) == func.count(),
                    func.sum(txn.c.cad_amount),
                ),
                else_=cast(None, txn.c.cad_amount.type),
            ).label("cad_amount"),
        )
        .where(txn.c.movement_kind != literal("fiat"), txn.c.exclusion.is_(None))
        .group_by(
            txn.c.currency,
            txn.c.movement_kind,
            txn.c.transfer_state,
            txn.c.pair_kind,
        )
    )


def report_queue_counts_select() -> Select[tuple[object, ...]]:
    """Last derive snapshot dated as_of_date; zero rows means unavailable."""
    return select(queue_count.c.kind, queue_count.c.count, queue_count.c.as_of_date)
