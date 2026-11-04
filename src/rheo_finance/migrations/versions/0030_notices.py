"""Notice state and preserved parser overdrawn spells (FR-27, FR-55).

Parser spells retain printed-row semantics: inferred balances between the two
parts of a split row must never start an overdraft.
"""

from alembic import op

revision = "0030_notices"
down_revision = "0029_presumed_paid"
branch_labels = None
depends_on = None

STATEMENTS = (
    """
    CREATE TABLE finance.notice (
        key text PRIMARY KEY,
        kind text NOT NULL CHECK (kind IN
            ('alert', 'nudge', 'overdue', 'digest', 'alerts')),
        event_date date NOT NULL,
        first_seen_at timestamptz NOT NULL,
        state text NOT NULL CHECK (state IN
            ('pending', 'claimed', 'sent', 'failed', 'unknown',
             'suppressed', 'superseded')),
        sent_at timestamptz
    )
    """,
    """
    CREATE TABLE finance.parsed_overdrawn (
        document_id uuid NOT NULL REFERENCES finance.document (id),
        account_id uuid NOT NULL REFERENCES finance.account (id)
            DEFERRABLE INITIALLY DEFERRED,
        currency text NOT NULL,
        start_date date NOT NULL,
        end_date date,
        PRIMARY KEY (document_id, account_id, currency, start_date),
        CHECK (end_date IS NULL OR end_date >= start_date)
    )
    """,
    """
    CREATE TABLE finance.overdrawn_spell (
        account_id uuid NOT NULL REFERENCES finance.account (id)
            DEFERRABLE INITIALLY DEFERRED,
        currency text NOT NULL,
        start_date date NOT NULL,
        end_date date,
        PRIMARY KEY (account_id, currency, start_date),
        CHECK (end_date IS NULL OR end_date >= start_date)
    )
    """,
)


def upgrade() -> None:
    for statement in STATEMENTS:
        op.execute(statement)


def downgrade() -> None:
    raise RuntimeError("finance migrations are forward-only")
