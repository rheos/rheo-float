"""An invoice's printed amount due, and the decision a currency refused (FR-8, FR-19
path (5); SPEC Derivation stages 3 and 14; AC-8).

``document_line.amount_due`` is what an invoice prints as still due: what an
``invoice_unpaid`` debt owes. The line's ``amount`` stays the total, which a payment
matches. The invoice parser's version moved with it, so a stored parse of an invoice is
redone on the next derive.

``document_line.refused_decision_id`` is the invoice decision stage 3 could not apply,
because neither the internal account nor an obligation holds the invoice's currency.
Stage 3 writes it, and a derive clears it first.

``VIEWS`` is frozen SQL (see 0014). ``report_status`` keeps its columns and its grant,
and gains one fact per decision kind, ``invoice_decision_refused``.
"""

from alembic import op

revision = "0025_invoice_amount_due"
down_revision = "0024_invoice_decisions"
branch_labels = None
depends_on = None

STATEMENTS = (
    "ALTER TABLE finance.document_line ADD COLUMN amount_due numeric",
    """
    ALTER TABLE finance.document_line
        ADD COLUMN refused_decision_id uuid REFERENCES finance.decision (id)
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
        GROUP BY finance.decision.kind UNION ALL SELECT
        'invoice_decision_refused' AS fact, finance.decision.kind AS
        subject, count(*) AS count, CAST(NULL AS TIMESTAMP WITH TIME
        ZONE) AS at FROM finance.document_line JOIN finance.decision ON
        finance.document_line.refused_decision_id = finance.decision.id
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
