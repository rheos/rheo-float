"""Schema grants only: no tables yet.

``finance db init`` creates schema ``finance`` (owned by ``finance_owner``) and the
roles. This revision pins the schema's grants in the chain, so a database migrated
without ``db init`` gets them too: ``PUBLIC`` has nothing on the schema and the reader
has USAGE only. Report views grant the reader SELECT in their own revisions.
"""

from alembic import op

revision = "0001_finance_schema"
down_revision = None
branch_labels = None
depends_on = None

STATEMENTS = (
    "REVOKE ALL ON SCHEMA finance FROM PUBLIC",
    "GRANT USAGE ON SCHEMA finance TO finance_reader",
)


def upgrade() -> None:
    for statement in STATEMENTS:
        op.execute(statement)


def downgrade() -> None:
    raise RuntimeError("finance migrations are forward-only")
