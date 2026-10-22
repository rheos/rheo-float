"""The derived ``sink_spec`` table (derivation stage 16).

One row per sink with a registered provider: its ``spec_digest`` and the spec's parts
as public text (hosts and request templates one per line, data inputs as
``name=value`` lines). Derive rewrites it; nothing else writes it. The reader gets no
grant here: the report views that read it grant SELECT in their own revisions.
"""

from alembic import op

revision = "0003_sink_spec"
down_revision = "0002_decisions"
branch_labels = None
depends_on = None

STATEMENTS = (
    """
    CREATE TABLE finance.sink_spec (
        sink text PRIMARY KEY,
        spec_digest text NOT NULL CHECK (spec_digest ~ '^[0-9a-f]{64}$'),
        hosts text NOT NULL,
        request_templates text NOT NULL,
        key_file text,
        policy_version integer NOT NULL CHECK (policy_version >= 0),
        data_inputs text NOT NULL,
        computed_at timestamptz NOT NULL DEFAULT now()
    )
    """,
)


def upgrade() -> None:
    for statement in STATEMENTS:
        op.execute(statement)


def downgrade() -> None:
    raise RuntimeError("finance migrations are forward-only")
