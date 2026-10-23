"""What a document's stored parse output was computed from: ``document.parse_inputs``.

``finance.ledger.derive`` keeps the stored parse output (``document_line``,
``statement_period``, ``rejected_period`` and the rejection event) of a document whose
bytes, parsers and account registry have not moved since it was parsed, and parses the
rest. The bytes are the document's sha256, already stored; ``parse_inputs`` holds the
sha256 of the other two at the time of the parse: the issuer and format, the id and
version of every parser registered for them, and the account registry the parsers
matched against (``derive.stage1.parse_inputs``). A null means the document has no
stored parse to reuse. ``finance.ledger.rebuild`` parses every document again and
rewrites it.

A hash of code and settings, not of anything the owner wrote: tier public. The next
free number after 0012 is 0013. No reader grant.
"""

from alembic import op

revision = "0013_parse_inputs"
down_revision = "0012_coverage"
branch_labels = None
depends_on = None

STATEMENTS = (
    """
    ALTER TABLE finance.document
        ADD COLUMN parse_inputs text CHECK (parse_inputs ~ '^[0-9a-f]{64}$')
    """,
)


def upgrade() -> None:
    for statement in STATEMENTS:
        op.execute(statement)


def downgrade() -> None:
    raise RuntimeError("finance migrations are forward-only")
