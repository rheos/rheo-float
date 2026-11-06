"""``payee``: the payees stage 2 loads (FR-10, FR-46; SPEC "Data Models", Derivation
stage 2).

One row per payee (``manual.payees``): the account-backed payees of ``accounts.csv``
``payee_patterns``, the rows of ``payees.csv``, and a vendor payee for each bill
issuer no other row holds. ``slug`` and ``display_label`` are the owner's words
(public); ``patterns`` are printed names (restricted). ``account_id`` is set for kind
``account`` only, and its key is deferred to commit, as the parse cache's are: stage 2
rewrites ``account`` in the same transaction. ``party_ref`` is plain text: the party
slug as written, which nothing resolves until the peer-money feature defines parties.
Stage 2 rewrites the table whole.

No reader grant: report views grant SELECT in their own revisions.
"""

from alembic import op

revision = "0020_payee"
down_revision = "0019_crypto_label"
branch_labels = None
depends_on = None

STATEMENTS = (
    """
    CREATE TABLE finance.payee (
        id uuid PRIMARY KEY,
        slug text NOT NULL UNIQUE CHECK (slug ~ '^[a-z0-9][a-z0-9_-]*$'),
        display_label text NOT NULL CHECK (display_label <> ''),
        kind text NOT NULL CHECK (kind IN ('vendor', 'person', 'account')),
        account_id uuid REFERENCES finance.account (id)
            DEFERRABLE INITIALLY DEFERRED,
        party_ref text CHECK (party_ref <> ''),
        patterns text[] NOT NULL DEFAULT '{}',
        CHECK ((kind = 'account') = (account_id IS NOT NULL))
    )
    """,
)


def upgrade() -> None:
    for statement in STATEMENTS:
        op.execute(statement)


def downgrade() -> None:
    raise RuntimeError("finance migrations are forward-only")
