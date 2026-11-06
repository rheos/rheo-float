"""Durable job history and its bounded reader projection."""

from alembic import op

revision = "0040_job_run"
down_revision = "0039_reader_boundaries"
branch_labels = None
depends_on = None

VIEWS = {
    "report_job": """
        SELECT latest.job, (SELECT max(finance.job_run.finished_at) AS max_1
        FROM finance.job_run WHERE finance.job_run.job = latest.job AND
        finance.job_run.ok IS true) AS last_success, latest.detail_code AS last_detail
        FROM (SELECT DISTINCT ON (finance.job_run.job) finance.job_run.job AS job,
        finance.job_run.detail_code AS detail_code FROM finance.job_run ORDER BY
        finance.job_run.job, finance.job_run.finished_at DESC, finance.job_run.id DESC)
        AS latest
    """,
}


def upgrade() -> None:
    op.execute("""
        CREATE TABLE finance.job_run (
            id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            job text NOT NULL CHECK (job ~ '^finance[.][a-z_]+$'),
            started_at timestamptz NOT NULL,
            finished_at timestamptz NOT NULL CHECK (finished_at >= started_at),
            ok boolean,
            detail_code text NOT NULL CHECK (
                (ok IS TRUE AND detail_code = 'completed') OR
                (ok IS FALSE AND detail_code = 'failed') OR
                (ok IS NULL AND detail_code IN (
                    'skipped: sink off', 'pending: current not configured', 'pending'
                ))
            ),
            input_digest text CHECK (input_digest ~ '^[a-f0-9]{64}$')
        )
    """)
    op.execute("ALTER TABLE finance.job_run OWNER TO finance_owner")
    op.execute("REVOKE ALL ON finance.job_run FROM finance_reader")
    for name, body in VIEWS.items():
        op.execute(f"CREATE VIEW finance.{name} AS {body}")
        op.execute(f"ALTER VIEW finance.{name} OWNER TO finance_owner")
        op.execute(f"GRANT SELECT ON finance.{name} TO finance_reader")


def downgrade() -> None:
    raise RuntimeError("finance migrations are forward-only")
