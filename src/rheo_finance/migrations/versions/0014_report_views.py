"""The status, coverage and document report views: ``report_status``,
``report_coverage`` and ``report_document`` (FR-48, FR-51).

All three are owned by ``finance_owner`` (the role that migrates) and the reader gets
SELECT on them in this revision; the reader holds no other privilege, on these or on
any base table.

``VIEWS`` is frozen SQL: the bodies ``rheo_finance.reports.views`` compiled to when
this revision was written, wrapped at its spaces. The frozen-views guard compares what
the code compiles to now with the newest revision's ``VIEWS`` entry for each view
(whitespace aside), so a view changed in code fails until a new revision recreates it.

No view shows an account identifier, a description, a merchant key, a counterparty or
the path of a skipped file. The next free number after 0013 is 0014.
"""

from alembic import op

revision = "0014_report_views"
down_revision = "0013_parse_inputs"
branch_labels = None
depends_on = None

VIEWS = {
    "report_status": """
        SELECT facts.fact, facts.subject, facts.count, facts.at FROM (SELECT 'schema' AS
        fact, finance.alembic_version_finance.version_num AS subject, CAST(1 AS BIGINT)
        AS count, CAST(NULL AS TIMESTAMP WITH TIME ZONE) AS at FROM
        finance.alembic_version_finance UNION ALL SELECT 'last_import' AS fact,
        split_part(finance.document.issuer, '/', 1) AS subject, count(*) AS count,
        max(finance.document.imported_at) AS at FROM finance.document GROUP BY
        split_part(finance.document.issuer, '/', 1) UNION ALL SELECT 'document_status'
        AS fact, finance.document.status AS subject, count(*) AS count, CAST(NULL AS
        TIMESTAMP WITH TIME ZONE) AS at FROM finance.document GROUP BY
        finance.document.status UNION ALL SELECT 'source_discrepancy' AS fact,
        finance.source_discrepancy.kind AS subject, count(*) AS count, CAST(NULL AS
        TIMESTAMP WITH TIME ZONE) AS at FROM finance.source_discrepancy GROUP BY
        finance.source_discrepancy.kind UNION ALL SELECT 'archive_skip_identifier' AS
        fact, finance.archive_skip.directory AS subject, CAST(finance.archive_skip.count
        AS BIGINT) AS count, CAST(NULL AS TIMESTAMP WITH TIME ZONE) AS at FROM
        finance.archive_skip WHERE finance.archive_skip.reason = 'identifier_in_path'
        UNION ALL SELECT 'account_no_data' AS fact, finance.account.label AS subject,
        CAST(1 AS BIGINT) AS count, CAST(NULL AS TIMESTAMP WITH TIME ZONE) AS at FROM
        finance.account WHERE NOT (EXISTS (SELECT finance.coverage.account_id FROM
        finance.coverage WHERE finance.coverage.account_id = finance.account.id)) AND
        finance.account.slug != 'paid-outside' UNION ALL SELECT 'missing_months' AS
        fact, finance.account.label AS subject, count(*) AS count, CAST(NULL AS
        TIMESTAMP WITH TIME ZONE) AS at FROM finance.coverage JOIN finance.account ON
        finance.coverage.account_id = finance.account.id WHERE finance.coverage.state =
        'missing' GROUP BY finance.account.id, finance.account.label) AS facts
    """,
    "report_coverage": """
        SELECT finance.account.slug AS account, finance.account.label AS account_label,
        finance.coverage.month, finance.coverage.state,
        replace(finance.coverage.reason_code, '_', ' ') AS reason FROM finance.coverage
        JOIN finance.account ON finance.coverage.account_id = finance.account.id
    """,
    "report_document": """
        SELECT finance.document.path, finance.document.kind, finance.document.status,
        finance.document.period_start, finance.document.period_end,
        finance.document.official, finance.document.extraction FROM finance.document
    """,
}


def statements() -> list[str]:
    out = [f"CREATE VIEW finance.{name} AS {body}" for name, body in VIEWS.items()]
    out.append(
        "GRANT SELECT ON finance.report_status, finance.report_coverage,"
        " finance.report_document TO finance_reader"
    )
    return out


def upgrade() -> None:
    for statement in statements():
        op.execute(statement)


def downgrade() -> None:
    raise RuntimeError("finance migrations are forward-only")
