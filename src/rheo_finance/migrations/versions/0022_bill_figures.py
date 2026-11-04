"""What stage 14's paths (3), (4) and (5) add to ``statement_period``, ``obligation``
and ``report_status`` (FR-19; SPEC "Data Models", Derivation stage 14, status line 9;
AC-87).

``statement_period.current_charges`` and ``arrears`` are what a bill prints, each inside
its ``balance_due``: while an arrangement is active the bill is owed its current charges
only, and a printed arrears line becomes an ``arrears`` obligation when none is. The
bill parsers' versions moved with them, so a stored parse of a bill is redone on the
next derive.

``obligation.manual_source`` is the ``obligations.csv`` row of an obligation that has
one: a manual row merged into a document stays a source, and ``primary_source`` holds
the document. ``statement_conflict`` is true when two parsed statements of one card
with one closing date differ in their figures.

``VIEWS`` is frozen SQL (see 0014). ``report_status`` keeps its columns and its grant,
and gains two facts per payee, ``manual_mismatch`` and ``statement_conflict``.
"""

from alembic import op

revision = "0022_bill_figures"
down_revision = "0021_obligation"
branch_labels = None
depends_on = None

STATEMENTS = (
    "ALTER TABLE finance.statement_period ADD COLUMN current_charges numeric",
    "ALTER TABLE finance.statement_period ADD COLUMN arrears numeric",
    """
    ALTER TABLE finance.obligation
        ADD COLUMN manual_source text CHECK (manual_source <> ''),
        ADD COLUMN statement_conflict boolean NOT NULL DEFAULT false
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
        finance.payee.id, finance.payee.display_label) AS facts
    """,
}


def upgrade() -> None:
    for statement in STATEMENTS:
        op.execute(statement)
    for name, body in VIEWS.items():
        op.execute(f"CREATE OR REPLACE VIEW finance.{name} AS {body}")


def downgrade() -> None:
    raise RuntimeError("finance migrations are forward-only")
