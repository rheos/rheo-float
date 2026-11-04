"""``obligation`` (derived, stage 14), the bill date on ``statement_period``, and the
duplicate bill months in ``report_status`` (FR-19, FR-21; SPEC "Data Models",
Derivation stage 14, status line 9).

``obligation`` is keyed by a derived id of its natural key (payee, kind, period label),
and that key is unique too. ``amount_due`` and ``threshold_due`` are as printed, so a
card in credit holds a negative figure; ``remaining`` and ``threshold_remaining`` never
go below zero. ``manual_mismatch`` and ``payments_mismatch`` each hold a difference in
CAD. ``supersedes_id`` is on the newer row and names the row it replaced. The payee key
is deferred to commit, as ``payee``'s account key is: stage 2 rewrites ``payee`` in the
same transaction. The key to ``supersedes_id`` is deferred so a stage may insert a chain
in any order.

``statement_period.bill_date`` is the date a bill prints. A document's period does not
hold it: a FortisBC bill's period ends before its bill date. The bill parsers' versions
moved with it, so a stored parse of a bill is redone on the next derive.

``VIEWS`` is frozen SQL (see 0014). ``report_status`` keeps its columns and its grant,
and gains the fact ``duplicate_bill_month``: per payee, its bills whose period label
carries a ``-<n>`` suffix.

No reader grant on the table: report views grant SELECT in their own revisions.
"""

from alembic import op

revision = "0021_obligation"
down_revision = "0020_payee"
branch_labels = None
depends_on = None

STATEMENTS = (
    "ALTER TABLE finance.statement_period ADD COLUMN bill_date date",
    """
    CREATE TABLE finance.obligation (
        id uuid PRIMARY KEY,
        payee_id uuid NOT NULL REFERENCES finance.payee (id)
            DEFERRABLE INITIALLY DEFERRED,
        kind text NOT NULL CHECK (kind IN (
            'card_statement', 'bill', 'arrangement_instalment', 'arrears',
            'manual_debt'
        )),
        period_label text NOT NULL CHECK (period_label <> ''),
        statement_date date,
        issued_on date,
        amount_due numeric(20,2) NOT NULL,
        minimum_due numeric(20,2),
        threshold_due numeric(20,2) NOT NULL,
        paid numeric(20,2) NOT NULL CHECK (paid >= 0),
        remaining numeric(20,2) NOT NULL CHECK (remaining >= 0),
        threshold_remaining numeric(20,2) NOT NULL CHECK (threshold_remaining >= 0),
        due_date date,
        status text NOT NULL CHECK (status IN (
            'open', 'partial', 'paid', 'presumed_paid', 'disputed', 'superseded'
        )),
        primary_source text NOT NULL CHECK (primary_source <> ''),
        manual_mismatch numeric(20,2),
        supersedes_id uuid REFERENCES finance.obligation (id)
            DEFERRABLE INITIALLY DEFERRED,
        arrangement_ref text CHECK (arrangement_ref <> ''),
        invoice_ref text CHECK (invoice_ref <> ''),
        payments_mismatch numeric(20,2),
        UNIQUE (payee_id, kind, period_label)
    )
    """,
)

VIEWS = {
    "report_status": """
        SELECT facts.fact, facts.subject, facts.count, facts.at FROM (SELECT
        'schema' AS fact, finance.alembic_version_finance.version_num AS
        subject, CAST(1 AS BIGINT) AS count, CAST(NULL AS TIMESTAMP WITH TIME
        ZONE) AS at FROM finance.alembic_version_finance UNION ALL SELECT
        'last_import' AS fact, split_part(finance.document.issuer, '/', 1) AS
        subject, count(*) AS count, max(finance.document.imported_at) AS at FROM
        finance.document GROUP BY split_part(finance.document.issuer, '/', 1)
        UNION ALL SELECT 'document_status' AS fact, finance.document.status AS
        subject, count(*) AS count, CAST(NULL AS TIMESTAMP WITH TIME ZONE) AS at
        FROM finance.document GROUP BY finance.document.status UNION ALL SELECT
        'source_discrepancy' AS fact, finance.source_discrepancy.kind AS
        subject, count(*) AS count, CAST(NULL AS TIMESTAMP WITH TIME ZONE) AS at
        FROM finance.source_discrepancy GROUP BY finance.source_discrepancy.kind
        UNION ALL SELECT 'archive_skip_identifier' AS fact,
        finance.archive_skip.directory AS subject,
        CAST(finance.archive_skip.count AS BIGINT) AS count, CAST(NULL AS
        TIMESTAMP WITH TIME ZONE) AS at FROM finance.archive_skip WHERE
        finance.archive_skip.reason = 'identifier_in_path' UNION ALL SELECT
        'account_no_data' AS fact, finance.account.label AS subject, CAST(1 AS
        BIGINT) AS count, CAST(NULL AS TIMESTAMP WITH TIME ZONE) AS at FROM
        finance.account WHERE NOT (EXISTS (SELECT finance.coverage.account_id
        FROM finance.coverage WHERE finance.coverage.account_id =
        finance.account.id)) AND finance.account.slug != 'paid-outside' UNION
        ALL SELECT 'missing_months' AS fact, finance.account.label AS subject,
        count(*) AS count, CAST(NULL AS TIMESTAMP WITH TIME ZONE) AS at FROM
        finance.coverage JOIN finance.account ON finance.coverage.account_id =
        finance.account.id WHERE finance.coverage.state = 'missing' GROUP BY
        finance.account.id, finance.account.label UNION ALL SELECT
        'duplicate_bill_month' AS fact, finance.payee.display_label AS subject,
        count(*) AS count, CAST(NULL AS TIMESTAMP WITH TIME ZONE) AS at FROM
        finance.obligation JOIN finance.payee ON finance.obligation.payee_id =
        finance.payee.id WHERE finance.obligation.kind = 'bill' AND
        finance.obligation.period_label ~ '^bill-[0-9]{4}-[0-9]{2}-' GROUP BY
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
