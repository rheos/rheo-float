"""A receipt payment's own reference, as a digest: ``document_line.payment_ref_digest``.

A receipt prints a number for each payment it lists. Two genuine receipts can print
the same payee, date, amount and payment type, and only that number tells them apart
from a re-saved copy of one receipt. The parser stores the first 16 hex digits of a
sha256 over the number, never the number itself, and stage 3 keys a receipt line by
it (``derive.evidence``). Null for every other line.

A short number's digest can be reversed by trying every number, so the column is
restricted. The next free number after 0014 is 0015. No reader grant.
"""

from alembic import op

revision = "0015_receipt_ref_digest"
down_revision = "0014_report_views"
branch_labels = None
depends_on = None

STATEMENTS = (
    """
    ALTER TABLE finance.document_line
        ADD COLUMN payment_ref_digest text
            CHECK (payment_ref_digest ~ '^[0-9a-f]{16}$')
    """,
)


def upgrade() -> None:
    for statement in STATEMENTS:
        op.execute(statement)


def downgrade() -> None:
    raise RuntimeError("finance migrations are forward-only")
