"""What the invoice decisions add (FR-8, FR-16; SPEC Derivation stage 3; AC-8).

``txn_source.overrides_decision_id`` is the invoice decision a matching payment took
over from: the invoice is evidence of the payment and the decision adds nothing, so it
never counts twice. Stage 3 sets it on the evidence source of the invoice line, the copy
that counts.

``VIEWS`` is frozen SQL (see 0014). ``report_status`` keeps its columns and its grant,
and gains one fact per decision kind, ``invoice_decision_superseded``.
"""

from alembic import op

revision = "0024_invoice_decisions"
down_revision = "0023_typed_bill_unjoined"
branch_labels = None
depends_on = None

STATEMENTS = (
    """
    ALTER TABLE finance.txn_source
        ADD COLUMN overrides_decision_id uuid REFERENCES finance.decision (id)
    """,
)

VIEWS = {
    "report_status": """
        SELECT facts.fact, facts.subject, facts.count, facts.at FROM
        (SELECT 'schema' AS fact,
        finance.alembic_version_finance.version_num AS subject, CAST(1
        AS BIGINT) AS count, CAST(NULL AS TIMESTAMP WITH TIME ZONE) AS
        at FROM finance.alembic_version_finance UNION ALL SELECT
        'last_import' AS fact, split_part(finance.document.issuer, '/',
        1) AS subject, count(*) AS count,
        max(finance.document.imported_at) AS at FROM finance.document
        GROUP BY split_part(finance.document.issuer, '/', 1) UNION ALL
        SELECT 'document_status' AS fact, finance.document.status AS
        subject, count(*) AS count, CAST(NULL AS TIMESTAMP WITH TIME
        ZONE) AS at FROM finance.document GROUP BY
        finance.document.status UNION ALL SELECT 'source_discrepancy' AS
        fact, finance.source_discrepancy.kind AS subject, count(*) AS
        count, CAST(NULL AS TIMESTAMP WITH TIME ZONE) AS at FROM
        finance.source_discrepancy GROUP BY
        finance.source_discrepancy.kind UNION ALL SELECT
        'archive_skip_identifier' AS fact,
        finance.archive_skip.directory AS subject,
        CAST(finance.archive_skip.count AS BIGINT) AS count, CAST(NULL
        AS TIMESTAMP WITH TIME ZONE) AS at FROM finance.archive_skip
        WHERE finance.archive_skip.reason = 'identifier_in_path' UNION
        ALL SELECT 'account_no_data' AS fact, finance.account.label AS
        subject, CAST(1 AS BIGINT) AS count, CAST(NULL AS TIMESTAMP WITH
        TIME ZONE) AS at FROM finance.account WHERE NOT (EXISTS (SELECT
        finance.coverage.account_id FROM finance.coverage WHERE
        finance.coverage.account_id = finance.account.id)) AND
        finance.account.slug != 'paid-outside' UNION ALL SELECT
        'missing_months' AS fact, finance.account.label AS subject,
        count(*) AS count, CAST(NULL AS TIMESTAMP WITH TIME ZONE) AS at
        FROM finance.coverage JOIN finance.account ON
        finance.coverage.account_id = finance.account.id WHERE
        finance.coverage.state = 'missing' GROUP BY finance.account.id,
        finance.account.label UNION ALL SELECT 'duplicate_bill_month' AS
        fact, finance.payee.display_label AS subject, count(*) AS count,
        CAST(NULL AS TIMESTAMP WITH TIME ZONE) AS at FROM
        finance.obligation JOIN finance.payee ON
        finance.obligation.payee_id = finance.payee.id WHERE
        finance.obligation.kind = 'bill' AND
        finance.obligation.period_label ~ '^bill-[0-9]{4}-[0-9]{2}-'
        GROUP BY finance.payee.id, finance.payee.display_label UNION ALL
        SELECT 'manual_mismatch' AS fact, finance.payee.display_label AS
        subject, count(*) AS count, CAST(NULL AS TIMESTAMP WITH TIME
        ZONE) AS at FROM finance.obligation JOIN finance.payee ON
        finance.obligation.payee_id = finance.payee.id WHERE
        finance.obligation.manual_mismatch IS NOT NULL GROUP BY
        finance.payee.id, finance.payee.display_label UNION ALL SELECT
        'statement_conflict' AS fact, finance.payee.display_label AS
        subject, count(*) AS count, CAST(NULL AS TIMESTAMP WITH TIME
        ZONE) AS at FROM finance.obligation JOIN finance.payee ON
        finance.obligation.payee_id = finance.payee.id WHERE
        finance.obligation.statement_conflict IS true GROUP BY
        finance.payee.id, finance.payee.display_label UNION ALL SELECT
        'typed_bill_unjoined' AS fact, finance.payee.display_label AS
        subject, count(*) AS count, CAST(NULL AS TIMESTAMP WITH TIME
        ZONE) AS at FROM finance.obligation JOIN finance.payee ON
        finance.obligation.payee_id = finance.payee.id WHERE
        finance.obligation.unjoined IS true GROUP BY finance.payee.id,
        finance.payee.display_label UNION ALL SELECT
        'invoice_decision_superseded' AS fact, finance.decision.kind AS
        subject, count(*) AS count, CAST(NULL AS TIMESTAMP WITH TIME
        ZONE) AS at FROM finance.txn_source JOIN finance.decision ON
        finance.txn_source.overrides_decision_id = finance.decision.id
        GROUP BY finance.decision.kind) AS facts
    """,
}


def upgrade() -> None:
    for statement in STATEMENTS:
        op.execute(statement)
    for name, body in VIEWS.items():
        op.execute(f"CREATE OR REPLACE VIEW finance.{name} AS {body}")


def downgrade() -> None:
    raise RuntimeError("finance migrations are forward-only")
