"""Durable Current period claims and acknowledged desired state."""

from alembic import op

revision = "0036_current_sink"
down_revision = "0035_owed"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE finance.sink_ticket (
            source_ref text PRIMARY KEY,
            external_id text UNIQUE,
            state text NOT NULL CHECK (state IN ('post_pending', 'open', 'done')),
            create_title text NOT NULL CHECK (length(create_title) BETWEEN 1 AND 500),
            create_body text NOT NULL CHECK (length(create_body) <= 20000),
            create_due_on date NOT NULL,
            create_priority text NOT NULL CHECK (create_priority IN ('normal', 'high')),
            last_title text,
            last_due_on date,
            last_priority text CHECK (last_priority IN ('normal', 'high')),
            last_status text CHECK (last_status IN ('open', 'done')),
            last_snoozed_until timestamptz,
            settled_at timestamptz,
            last_attempt_at timestamptz,
            last_success_at timestamptz,
            last_error_code text,
            CHECK (state != 'post_pending' OR external_id IS NULL),
            CHECK (state != 'open' OR external_id IS NOT NULL)
        )
    """)
    op.execute("ALTER TABLE finance.sink_ticket OWNER TO finance_owner")


def downgrade() -> None:
    raise RuntimeError("finance migrations are forward-only")
