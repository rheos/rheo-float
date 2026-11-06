"""Preserve separate parser spells that share their printed dates.

Existing cache rows receive an ordinal in start-date order; the previous key
allowed at most one start per account and currency in a document. Derived spells
are cleared and rebuilt by the next derive from these retained cache facts.
"""

from alembic import op

revision = "0031_overdrawn_identity"
down_revision = "0030_notices"
branch_labels = None
depends_on = None

STATEMENTS = (
    "ALTER TABLE finance.parsed_overdrawn ADD COLUMN spell_no integer",
    # Backfill updates must not leave deferred FK trigger events before ALTER.
    "SET CONSTRAINTS finance.parsed_overdrawn_account_id_fkey IMMEDIATE",
    """
    WITH numbered AS (
        SELECT document_id, account_id, currency, start_date,
            row_number() OVER (
                PARTITION BY document_id, account_id, currency ORDER BY start_date
            ) AS spell_no
        FROM finance.parsed_overdrawn
    )
    UPDATE finance.parsed_overdrawn AS stored SET spell_no = numbered.spell_no
    FROM numbered
    WHERE stored.document_id = numbered.document_id
        AND stored.account_id = numbered.account_id
        AND stored.currency = numbered.currency
        AND stored.start_date = numbered.start_date
    """,
    """
    ALTER TABLE finance.parsed_overdrawn
        DROP CONSTRAINT parsed_overdrawn_pkey,
        ALTER COLUMN spell_no SET NOT NULL,
        ADD PRIMARY KEY (document_id, account_id, currency, spell_no),
        ADD CHECK (spell_no > 0)
    """,
    "DELETE FROM finance.overdrawn_spell",
    """
    ALTER TABLE finance.overdrawn_spell
        DROP CONSTRAINT overdrawn_spell_pkey,
        ADD COLUMN document_id uuid NOT NULL REFERENCES finance.document (id),
        ADD COLUMN spell_no integer NOT NULL CHECK (spell_no > 0),
        ADD PRIMARY KEY (document_id, account_id, currency, spell_no)
    """,
    "SET CONSTRAINTS finance.parsed_overdrawn_account_id_fkey DEFERRED",
)


def upgrade() -> None:
    for statement in STATEMENTS:
        op.execute(statement)


def downgrade() -> None:
    raise RuntimeError("finance migrations are forward-only")
