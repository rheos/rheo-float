"""``archive_skip`` reason ``unreadable``: a file or directory the walk could not read.

The walk counts an entry that vanished or could not be opened instead of failing on
it. Revision 0007 is frozen, so its reason CHECK is replaced here.
"""

from alembic import op

revision = "0008_archive_skip_unreadable"
down_revision = "0007_documents"
branch_labels = None
depends_on = None

STATEMENTS = (
    "ALTER TABLE finance.archive_skip DROP CONSTRAINT archive_skip_reason_check",
    """
    ALTER TABLE finance.archive_skip ADD CONSTRAINT archive_skip_reason_check
        CHECK (reason IN ('identifier_in_path', 'unmapped_directory',
            'not_a_regular_file', 'not_downloaded', 'unreadable'))
    """,
)


def upgrade() -> None:
    for statement in STATEMENTS:
        op.execute(statement)


def downgrade() -> None:
    raise RuntimeError("finance migrations are forward-only")
