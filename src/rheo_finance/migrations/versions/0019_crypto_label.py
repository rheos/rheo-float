"""``crypto_label``: the owner's labels for crypto movements nothing pairs (FR-15(b);
SPEC "Files, paths and settings", Derivation stages 2 and 11).

One row per labelled line, from ``crypto_labels.csv``: ``line_key_id`` the line key
the row's ``finance.txn:<uuid>`` names (resolved through ``txn_source.line_key_id``),
``label`` one of ``own_untracked`` and ``other``, and an optional ``note``
(restricted). Stage 2 rewrites it whole; stages 9 and 11 read it.

No reader grant: report views grant SELECT in their own revisions.
"""

from alembic import op

revision = "0019_crypto_label"
down_revision = "0018_crypto_core"
branch_labels = None
depends_on = None

STATEMENTS = (
    """
    CREATE TABLE finance.crypto_label (
        line_key_id uuid PRIMARY KEY,
        label text NOT NULL CHECK (label IN ('own_untracked', 'other')),
        note text CHECK (note <> '')
    )
    """,
)


def upgrade() -> None:
    for statement in STATEMENTS:
        op.execute(statement)


def downgrade() -> None:
    raise RuntimeError("finance migrations are forward-only")
