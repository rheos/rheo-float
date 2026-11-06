"""Keep UUIDv7 state identity and an internal run sequence for timestamp ties."""

from alembic import op

revision = "0041_job_identity"
down_revision = "0040_job_run"
branch_labels = None
depends_on = None

VIEWS = {
    "report_job": """
        SELECT latest.job, (SELECT max(finance.job_run.finished_at) AS max_1
        FROM finance.job_run WHERE finance.job_run.job = latest.job AND
        finance.job_run.ok IS true) AS last_success, latest.detail_code AS last_detail
        FROM (SELECT DISTINCT ON (finance.job_run.job) finance.job_run.job AS job,
        finance.job_run.detail_code AS detail_code FROM finance.job_run ORDER BY
        finance.job_run.job, finance.job_run.finished_at DESC, finance.job_run.seq DESC)
        AS latest
    """,
}


def upgrade() -> None:
    op.execute("DROP VIEW finance.report_job")
    op.execute("ALTER TABLE finance.job_run RENAME COLUMN id TO seq")
    op.execute("ALTER TABLE finance.job_run DROP CONSTRAINT job_run_pkey")
    op.execute("ALTER TABLE finance.job_run ADD COLUMN id uuid")
    # Preserve any history from 0040. PostgreSQL 17 has no uuidv7(), so form the
    # RFC 9562 layout from the stored start time and fresh platform random bits.
    op.execute("""
        WITH identities AS (
            SELECT seq,
                lpad(to_hex(floor(extract(epoch FROM started_at) * 1000)::bigint),
                     12, '0') AS stamp,
                gen_random_uuid()::text AS random
            FROM finance.job_run
        )
        UPDATE finance.job_run AS run SET id = (
            substr(identities.stamp, 1, 8) || '-' ||
            substr(identities.stamp, 9, 4) || '-7' ||
            substr(identities.random, 16, 3) || substr(identities.random, 19)
        )::uuid FROM identities WHERE run.seq = identities.seq
    """)
    op.execute("ALTER TABLE finance.job_run ALTER COLUMN id SET NOT NULL")
    op.execute("ALTER TABLE finance.job_run ADD PRIMARY KEY (id)")
    op.execute("ALTER TABLE finance.job_run ADD UNIQUE (seq)")
    for name, body in VIEWS.items():
        op.execute(f"CREATE VIEW finance.{name} AS {body}")
        op.execute(f"ALTER VIEW finance.{name} OWNER TO finance_owner")
        op.execute(f"GRANT SELECT ON finance.{name} TO finance_reader")


def downgrade() -> None:
    raise RuntimeError("finance migrations are forward-only")
