"""The parse cache and the event table: ``statement_period``, ``document_line``,
``event``.

``statement_period`` holds each document's own control data, one row per account and
currency. ``document_line`` holds every line a parser read (the parse cache). Both are
derived: parsing a document deletes and rewrites its rows, so a rebuild reproduces
them. ``event`` holds derived events, one id per event key; ``data`` is the one JSONB
column in the schema, rendered and never searched.

The account foreign keys are deferred to commit: stage 2 rewrites ``account`` in the
same transaction that reparses, and the account ids are derived from slugs, so an
account that is still registered gets the same id back before the check runs.

The prompt numbered this revision 0007; 0007 and 0008 were taken, so it is 0009. No
reader grant: report views grant SELECT in their own revisions.
"""

from alembic import op

revision = "0009_parse_cache"
down_revision = "0008_archive_skip_unreadable"
branch_labels = None
depends_on = None

STATEMENTS = (
    """
    CREATE TABLE finance.statement_period (
        document_id uuid NOT NULL REFERENCES finance.document (id),
        account_id uuid NOT NULL REFERENCES finance.account (id)
            DEFERRABLE INITIALLY DEFERRED,
        currency text NOT NULL CHECK (currency ~ '^[A-Z0-9]{2,12}$'),
        period_start date NOT NULL,
        period_end date NOT NULL,
        opening numeric,
        closing numeric,
        total_debits numeric,
        total_credits numeric,
        total_payments numeric,
        balance_due numeric,
        minimum_due numeric,
        due_date date,
        control_kind text NOT NULL CHECK (control_kind IN ('summary',
            'running_balance', 'bill_identity', 'none')),
        tied_out boolean NOT NULL,
        PRIMARY KEY (document_id, account_id, currency),
        CHECK (period_end >= period_start)
    )
    """,
    """
    CREATE TABLE finance.document_line (
        id uuid PRIMARY KEY,
        document_id uuid NOT NULL REFERENCES finance.document (id),
        line_no integer NOT NULL CHECK (line_no >= 1),
        page integer CHECK (page >= 1),
        line_kind text NOT NULL CHECK (line_kind IN ('transaction',
            'payment_notice', 'invoice', 'receipt', 'crypto_movement', 'crypto_fee',
            'holding', 'notice_fact', 'trading_result')),
        account_id uuid REFERENCES finance.account (id) DEFERRABLE INITIALLY DEFERRED,
        posting_date date,
        txn_date date,
        occurred_at timestamptz,
        amount numeric,
        currency text,
        asset_identifier text,
        original_amount numeric,
        original_currency text,
        stated_cad numeric(20, 2),
        stated_cad_price numeric,
        description_raw text,
        description_norm text,
        counterparty text,
        txid text,
        from_address text,
        to_address text,
        source_type text,
        is_fake boolean NOT NULL DEFAULT false,
        fee_stated boolean NOT NULL DEFAULT false,
        running_balance numeric,
        fee_type text,
        transfer_hint boolean NOT NULL DEFAULT false,
        reversal_hint boolean NOT NULL DEFAULT false,
        conversion_hint boolean NOT NULL DEFAULT false,
        etransfer_hint boolean NOT NULL DEFAULT false,
        hint_account_slug text,
        trade_ref text,
        instrument text,
        result numeric,
        trading_fee numeric,
        funding_fee numeric,
        line_key_id uuid,
        UNIQUE (document_id, line_no)
    )
    """,
    """
    CREATE TABLE finance.event (
        id uuid PRIMARY KEY,
        type text NOT NULL
            CHECK (type ~ '^finance[.][a-z][a-z0-9_]*[.][a-z][a-z0-9_]*$'),
        subject_ref text NOT NULL CHECK (subject_ref <> ''),
        occurred_on date NOT NULL,
        data jsonb NOT NULL CHECK (jsonb_typeof(data) = 'object')
    )
    """,
)


def upgrade() -> None:
    for statement in STATEMENTS:
        op.execute(statement)


def downgrade() -> None:
    raise RuntimeError("finance migrations are forward-only")
