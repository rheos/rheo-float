"""Safe line reports and aggregate crypto movements. Frozen SQL, forward-only."""

from alembic import op

revision = "0037_reader_views"
down_revision = "0036_current_sink"
branch_labels = None
depends_on = None

VIEWS = {
    "report_txn": """
        SELECT concat('finance.txn:', primary_source.line_key_id) AS line_ref,
        finance.account.slug AS account, finance.account.label AS account_label,
        finance.txn.posting_date, finance.txn.txn_date, finance.txn.occurred_at,
        finance.txn.recognition_date, finance.txn.amount, finance.txn.currency,
        finance.txn.cad_amount, finance.txn.basis, finance.txn.provisional,
        coalesce(finance.txn.display_label, 'Transaction') AS display_label,
        finance.txn.category, finance.txn.category_basis_class, finance.txn.tag,
        finance.txn.business_unit, finance.txn.business_pct,
        finance.txn.tag_basis_class, finance.txn.transfer_state, finance.txn.pair_kind,
        finance.txn.allocation_state, finance.txn.exclusion, CASE WHEN
        finance.txn.orphan_candidate THEN 'decision orphaned' ELSE CAST(NULL AS TEXT)
        END AS decision_state, CASE WHEN (finance.txn.pair_kind = 'reversal' AND (EXISTS
        (SELECT finance.decision.id  FROM finance.decision JOIN finance.txn_source AS
        split_source ON finance.decision.target_ref = concat('finance.txn:',
        split_source.line_key_id)  WHERE split_source.txn_id = finance.txn.id AND
        split_source.role IN ('primary', 'additional') AND finance.decision.kind =
        'split' AND NOT (EXISTS (SELECT *  FROM finance.decision AS rev  WHERE rev.kind
        = 'reverse' AND (rev.reverses_id = finance.decision.id OR
        finance.decision.bulk_id IS NOT NULL AND rev.reverses_bulk_id =
        finance.decision.bulk_id)))))) THEN 'split kept, on a reversed line' ELSE
        CAST(NULL AS TEXT) END AS split_state  FROM finance.txn JOIN finance.account ON
        finance.account.id = finance.txn.account_id JOIN finance.txn_source AS
        primary_source ON primary_source.txn_id = finance.txn.id AND primary_source.role
        = 'primary'
    """,
    "report_crypto_movement": """
        SELECT finance.txn.currency, finance.txn.transfer_state,
        finance.txn.allocation_state, finance.txn.exclusion, count(*) AS count,
        sum(finance.txn.amount) AS amount, sum(finance.txn.cad_amount) AS cad_amount
        FROM finance.txn  WHERE finance.txn.movement_kind = 'crypto_transfer' GROUP BY
        finance.txn.currency, finance.txn.transfer_state, finance.txn.allocation_state,
        finance.txn.exclusion
    """,
}


def upgrade() -> None:
    for name, body in VIEWS.items():
        op.execute(f"CREATE VIEW finance.{name} AS {body}")
        op.execute(f"ALTER VIEW finance.{name} OWNER TO finance_owner")
        op.execute(f"GRANT SELECT ON finance.{name} TO finance_reader")


def downgrade() -> None:
    raise RuntimeError("finance migrations are forward-only")
