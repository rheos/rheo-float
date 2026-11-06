"""Last-derive queue count snapshot and bounded crypto reader aggregation."""

from alembic import op

revision = "0038_queue_snapshot"
down_revision = "0037_reader_views"
branch_labels = None
depends_on = None

VIEWS = {
    "report_crypto_movement": """
        SELECT finance.txn.currency, finance.txn.movement_kind,
        finance.txn.transfer_state AS state, count(*) AS count, sum(finance.txn.amount)
        AS amount, CASE WHEN (count(finance.txn.cad_amount) = count(*)) THEN
        sum(finance.txn.cad_amount) ELSE CAST(NULL AS NUMERIC(20, 2)) END AS cad_amount
        FROM finance.txn  WHERE finance.txn.movement_kind != 'fiat' AND
        finance.txn.exclusion IS NULL GROUP BY finance.txn.currency,
        finance.txn.movement_kind, finance.txn.transfer_state
    """,
    "report_queue_counts": """
        SELECT finance.queue_count.kind, finance.queue_count.count,
        finance.queue_count.as_of_date  FROM finance.queue_count
    """,
}


def upgrade() -> None:
    op.execute("""
        CREATE TABLE finance.queue_count (
            kind text PRIMARY KEY CHECK (kind IN (
                'pairing', 'held_notice', 'card_debit', 'payment',
                'presumed_paid', 'reattach', 'category'
            )),
            count bigint NOT NULL CHECK (count >= 0),
            as_of_date date NOT NULL
        )
    """)
    op.execute("ALTER TABLE finance.queue_count OWNER TO finance_owner")
    op.execute("DROP VIEW finance.report_crypto_movement")
    for name, body in VIEWS.items():
        op.execute(f"CREATE VIEW finance.{name} AS {body}")
        op.execute(f"ALTER VIEW finance.{name} OWNER TO finance_owner")
        op.execute(f"GRANT SELECT ON finance.{name} TO finance_reader")


def downgrade() -> None:
    raise RuntimeError("finance migrations are forward-only")
