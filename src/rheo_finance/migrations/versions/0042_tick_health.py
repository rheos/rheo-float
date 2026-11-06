"""Reader tick health facts and a fixed schema refusal history detail."""

from alembic import op

revision = "0042_tick_health"
down_revision = "0041_job_identity"
branch_labels = None
depends_on = None

VIEWS = {
    "report_status": """
        SELECT facts.fact, facts.subject, facts.count, facts.at FROM (SELECT
        'schema' AS fact, finance.alembic_version_finance.version_num AS subject,
        CAST(1 AS BIGINT) AS count, CAST(NULL AS TIMESTAMP WITH TIME ZONE) AS at
        FROM finance.alembic_version_finance UNION ALL SELECT 'decision_orphan' AS
        fact, CAST(NULL AS TEXT) AS subject, count(DISTINCT
        finance.decision_orphan.decision_id) AS count, CAST(NULL AS TIMESTAMP WITH
        TIME ZONE) AS at FROM finance.decision_orphan UNION ALL SELECT 'bill_gap' AS
        fact, finance.document.issuer AS subject, count(*) AS count, CAST(NULL AS
        TIMESTAMP WITH TIME ZONE) AS at FROM finance.document WHERE
        finance.document.kind IN ('bill', 'image') AND finance.document.status IN
        ('needs_manual_entry', 'unparsed') AND NOT (EXISTS (SELECT
        finance.obligation.id FROM finance.obligation WHERE
        finance.obligation.primary_source = finance.document.path)) GROUP BY
        finance.document.issuer UNION ALL SELECT 'last_import' AS fact,
        split_part(finance.document.issuer, '/', 1) AS subject, count(*) AS count,
        max(finance.document.imported_at) AS at FROM finance.document GROUP BY
        split_part(finance.document.issuer, '/', 1) UNION ALL SELECT
        'document_status' AS fact, finance.document.status AS subject, count(*) AS
        count, CAST(NULL AS TIMESTAMP WITH TIME ZONE) AS at FROM finance.document
        GROUP BY finance.document.status UNION ALL SELECT 'source_discrepancy' AS
        fact, finance.source_discrepancy.kind AS subject, count(*) AS count,
        CAST(NULL AS TIMESTAMP WITH TIME ZONE) AS at FROM finance.source_discrepancy
        GROUP BY finance.source_discrepancy.kind UNION ALL SELECT
        'archive_skip_identifier' AS fact, finance.archive_skip.directory AS
        subject, CAST(finance.archive_skip.count AS BIGINT) AS count, CAST(NULL AS
        TIMESTAMP WITH TIME ZONE) AS at FROM finance.archive_skip WHERE
        finance.archive_skip.reason = 'identifier_in_path' UNION ALL SELECT
        'account_no_data' AS fact, finance.account.label AS subject, CAST(1 AS
        BIGINT) AS count, CAST(NULL AS TIMESTAMP WITH TIME ZONE) AS at FROM
        finance.account WHERE NOT (EXISTS (SELECT finance.coverage.account_id FROM
        finance.coverage WHERE finance.coverage.account_id = finance.account.id))
        AND finance.account.slug != 'paid-outside' UNION ALL SELECT 'missing_months'
        AS fact, finance.account.label AS subject, count(*) AS count, CAST(NULL AS
        TIMESTAMP WITH TIME ZONE) AS at FROM finance.coverage JOIN finance.account
        ON finance.coverage.account_id = finance.account.id WHERE
        finance.coverage.state = 'missing' GROUP BY finance.account.id,
        finance.account.label UNION ALL SELECT 'duplicate_bill_month' AS fact,
        finance.payee.display_label AS subject, count(*) AS count, CAST(NULL AS
        TIMESTAMP WITH TIME ZONE) AS at FROM finance.obligation JOIN finance.payee
        ON finance.obligation.payee_id = finance.payee.id WHERE
        finance.obligation.kind = 'bill' AND finance.obligation.period_label ~
        '^bill-[0-9]{4}-[0-9]{2}-' GROUP BY finance.payee.id,
        finance.payee.display_label UNION ALL SELECT 'manual_mismatch' AS fact,
        finance.payee.display_label AS subject, count(*) AS count, CAST(NULL AS
        TIMESTAMP WITH TIME ZONE) AS at FROM finance.obligation JOIN finance.payee
        ON finance.obligation.payee_id = finance.payee.id WHERE
        finance.obligation.manual_mismatch IS NOT NULL GROUP BY finance.payee.id,
        finance.payee.display_label UNION ALL SELECT 'statement_conflict' AS fact,
        finance.payee.display_label AS subject, count(*) AS count, CAST(NULL AS
        TIMESTAMP WITH TIME ZONE) AS at FROM finance.obligation JOIN finance.payee
        ON finance.obligation.payee_id = finance.payee.id WHERE
        finance.obligation.statement_conflict IS true GROUP BY finance.payee.id,
        finance.payee.display_label UNION ALL SELECT 'typed_bill_unjoined' AS fact,
        finance.payee.display_label AS subject, count(*) AS count, CAST(NULL AS
        TIMESTAMP WITH TIME ZONE) AS at FROM finance.obligation JOIN finance.payee
        ON finance.obligation.payee_id = finance.payee.id WHERE
        finance.obligation.unjoined IS true GROUP BY finance.payee.id,
        finance.payee.display_label UNION ALL SELECT 'arrangement_unsettled' AS
        fact, finance.obligation.arrangement_ref AS subject, CAST(1 AS BIGINT) AS
        count, CAST(NULL AS TIMESTAMP WITH TIME ZONE) AS at FROM finance.obligation
        WHERE finance.obligation.arrangement_unsettled IS true GROUP BY
        finance.obligation.arrangement_ref UNION ALL SELECT
        'arrangement_unsettled_by' AS fact,
        concat(finance.obligation.arrangement_ref, ' finance.decision:',
        finance.obligation.unsettled_decision_id) AS subject, CAST(1 AS BIGINT) AS
        count, CAST(NULL AS TIMESTAMP WITH TIME ZONE) AS at FROM finance.obligation
        WHERE finance.obligation.unsettled_decision_id IS NOT NULL GROUP BY
        finance.obligation.arrangement_ref, finance.obligation.unsettled_decision_id
        UNION ALL SELECT 'invoice_decision_superseded' AS fact,
        finance.decision.kind AS subject, count(*) AS count, CAST(NULL AS TIMESTAMP
        WITH TIME ZONE) AS at FROM finance.txn_source JOIN finance.decision ON
        finance.txn_source.overrides_decision_id = finance.decision.id GROUP BY
        finance.decision.kind UNION ALL SELECT 'invoice_decision_refused' AS fact,
        finance.decision.kind AS subject, count(*) AS count, CAST(NULL AS TIMESTAMP
        WITH TIME ZONE) AS at FROM finance.document_line JOIN finance.decision ON
        finance.document_line.refused_decision_id = finance.decision.id GROUP BY
        finance.decision.kind UNION ALL SELECT 'tick' AS fact, CAST(NULL AS TEXT) AS
        subject, count(*) AS count, max(finance.job_run.finished_at) AS at FROM
        finance.job_run WHERE finance.job_run.job = 'finance.tick' UNION ALL SELECT
        'untied_card' AS fact, CAST(NULL AS TEXT) AS subject, count(*) AS count,
        CAST(NULL AS TIMESTAMP WITH TIME ZONE) AS at FROM finance.payment WHERE
        finance.payment.state = 'confirm_only' AND finance.payment.source = 'money'
        UNION ALL SELECT 'unapplied_credit' AS fact, CAST(NULL AS TEXT) AS subject,
        count(*) AS count, CAST(NULL AS TIMESTAMP WITH TIME ZONE) AS at FROM
        finance.open_item WHERE finance.open_item.kind = 'unapplied_credit' UNION
        ALL SELECT 'reminder_retry' AS fact, CAST(NULL AS TEXT) AS subject, count(*)
        AS count, CAST(NULL AS TIMESTAMP WITH TIME ZONE) AS at FROM
        finance.sink_ticket WHERE finance.sink_ticket.state = 'post_pending' OR
        finance.sink_ticket.last_error_code IS NOT NULL UNION ALL SELECT
        'reminder_attempt' AS fact, CAST(NULL AS TEXT) AS subject, count(*) AS
        count, min(finance.job_run.finished_at) AS at FROM finance.job_run WHERE
        finance.job_run.job = 'finance.reminders_sync' AND finance.job_run.ok IS
        false) AS facts
    """,
}


def upgrade() -> None:
    op.execute("ALTER TABLE finance.job_run DROP CONSTRAINT job_run_check1")
    op.execute("""
        ALTER TABLE finance.job_run ADD CONSTRAINT job_run_detail_code_check CHECK (
            (ok IS TRUE AND detail_code = 'completed') OR
            (ok IS FALSE AND (detail_code = 'failed' OR
                (job = 'finance.tick' AND detail_code = 'schema_behind'))) OR
            (ok IS NULL AND detail_code IN (
                'skipped: sink off', 'pending: current not configured', 'pending'
            ))
        )
    """)
    for name, body in VIEWS.items():
        op.execute(f"CREATE OR REPLACE VIEW finance.{name} AS {body}")
        op.execute(f"ALTER VIEW finance.{name} OWNER TO finance_owner")
        op.execute(f"GRANT SELECT ON finance.{name} TO finance_reader")


def downgrade() -> None:
    raise RuntimeError("finance migrations are forward-only")
