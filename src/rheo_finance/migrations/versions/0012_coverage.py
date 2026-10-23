"""The coverage matrix (derivation stage 6) and the accounts of rejected documents.

``coverage`` holds one row per account and calendar month from the account's first
known date: its state and, for some ``missing`` and ``partial`` months, a reason code.
The primary key is the "exactly one state per account-month" rule. ``month`` is the
first day of the month. A reason belongs to a state: ``export_period`` and
``mail_window`` explain a ``partial`` month, the others a ``missing`` one. The mail
reasons are filled by the mail intake stage; nothing writes them yet.

``rejected_period`` is parse cache, written with the rejection: for each registered
account a rejected document names, the period it states there. A rejected document
loads no period and no line, so without it coverage could not tell which account's
months to show as ``rejected``. A rejection that names no registered account (an
unknown or ambiguous one) has no row.

``txn.tax_year`` already exists, generated from ``recognition_date`` (0010); stage 5
fills ``recognition_date``.

The prompt numbered this revision 0009; 0009 to 0011 were taken, so it is 0012. Both
tables are derived and rebuilt by every derive; the account keys are deferred to
commit, as in the parse cache. No reader grant: report views grant SELECT in their
own revisions.
"""

from alembic import op

revision = "0012_coverage"
down_revision = "0011_overlap"
branch_labels = None
depends_on = None

STATEMENTS = (
    """
    CREATE TABLE finance.rejected_period (
        document_id uuid NOT NULL REFERENCES finance.document (id),
        account_id uuid NOT NULL REFERENCES finance.account (id)
            DEFERRABLE INITIALLY DEFERRED,
        period_start date NOT NULL,
        period_end date NOT NULL,
        PRIMARY KEY (document_id, account_id),
        CHECK (period_end >= period_start)
    )
    """,
    """
    CREATE TABLE finance.coverage (
        account_id uuid NOT NULL REFERENCES finance.account (id)
            DEFERRABLE INITIALLY DEFERRED,
        month date NOT NULL CHECK (month = date_trunc('month', month)::date),
        state text NOT NULL CHECK (state IN ('have', 'partial', 'missing',
            'rejected', 'unparsed', 'not_applicable')),
        reason_code text CHECK (reason_code IN ('no_raw_mail', 'mail_purged',
            'mail_window', 'export_period', 'running_balance_break')),
        PRIMARY KEY (account_id, month),
        CHECK (reason_code IS NULL
            OR (state = 'partial' AND reason_code IN ('export_period', 'mail_window'))
            OR (state = 'missing' AND reason_code IN ('no_raw_mail', 'mail_purged',
                'running_balance_break')))
    )
    """,
)


def upgrade() -> None:
    for statement in STATEMENTS:
        op.execute(statement)


def downgrade() -> None:
    raise RuntimeError("finance migrations are forward-only")
