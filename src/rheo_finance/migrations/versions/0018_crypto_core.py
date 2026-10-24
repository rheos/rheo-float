"""Crypto core: ``own_address``, and the ``quantity_mismatch`` source discrepancy
(FR-4, FR-15(b); SPEC "Data Models", Derivation stages 2 and 3).

``own_address`` holds the owner's own wallet addresses from ``own_addresses.csv``,
one row per address in its compared form (a hex address lower-cased, any other as
printed), with the account it belongs to. Stage 2 rewrites it whole; stage 7 reads
it. Every column is restricted. The account key is deferred to commit, as the parse
cache's are: stage 2 rewrites ``account`` in the same transaction.

``source_discrepancy.kind`` gains ``quantity_mismatch``: two sources of one crypto
line keyed by its TXID print different quantities. The line of the lower-ranked
source is an additional source of the txn and also this discrepancy; the
higher-ranked source's quantity stands.

No reader grant: report views grant SELECT in their own revisions.
"""

from alembic import op

revision = "0018_crypto_core"
down_revision = "0017_pair_edge"
branch_labels = None
depends_on = None

STATEMENTS = (
    """
    CREATE TABLE finance.own_address (
        address text PRIMARY KEY CHECK (address <> '' AND address !~ '\\s'),
        account_id uuid NOT NULL REFERENCES finance.account (id)
            DEFERRABLE INITIALLY DEFERRED
    )
    """,
    """
    ALTER TABLE finance.source_discrepancy
        DROP CONSTRAINT source_discrepancy_kind_check
    """,
    """
    ALTER TABLE finance.source_discrepancy
        ADD CONSTRAINT source_discrepancy_kind_check
            CHECK (kind IN ('unmatched_in_statement_window', 'line_key_taken',
                'quantity_mismatch'))
    """,
)


def upgrade() -> None:
    for statement in STATEMENTS:
        op.execute(statement)


def downgrade() -> None:
    raise RuntimeError("finance migrations are forward-only")
