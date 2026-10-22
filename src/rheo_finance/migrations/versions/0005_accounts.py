"""The account registry: ``account``, ``account_identifier`` and ``manual_entry_error``.

All three are derived tables. Derivation stage 2 (``manual.stage2.load_accounts``)
deletes and rewrites them from the manual-entry files on every derive, so nothing here
is state. ``account_identifier`` is restricted: its rows are what a statement is matched
by and appear in no report. The reader gets no grant here; report views grant SELECT in
their own revisions.

The CHECKs repeat the loader's row rules, so a row that slipped past the loader fails
the insert instead of landing.
"""

from alembic import op

revision = "0005_accounts"
down_revision = "0004_sink_views"
branch_labels = None
depends_on = None

STATEMENTS = (
    """
    CREATE TABLE finance.account (
        id uuid PRIMARY KEY,
        slug text NOT NULL UNIQUE CHECK (slug ~ '^[a-z0-9][a-z0-9_-]*$'),
        label text NOT NULL CHECK (label <> ''),
        institution text NOT NULL CHECK (institution <> ''),
        kind text NOT NULL CHECK (kind IN ('chequing', 'savings', 'credit_card',
            'wallet', 'exchange', 'payment_processor', 'bill_vendor',
            'crypto_wallet')),
        currencies text[] NOT NULL CHECK (cardinality(currencies) > 0),
        cadence text NOT NULL CHECK (cadence IN ('monthly', 'quarterly', 'yearly',
            'none')),
        owner text,
        opened_on date,
        closed_on date,
        first_known_on date,
        sourced_by text NOT NULL CHECK (sourced_by IN ('documents', 'mail', 'api')),
        placeholder boolean NOT NULL DEFAULT false,
        missing_before date,
        missing_reason text CHECK (missing_reason IN ('mail_purged')),
        CHECK (closed_on >= opened_on),
        CHECK (NOT placeholder OR kind = 'crypto_wallet'),
        CHECK ((missing_before IS NULL) = (missing_reason IS NULL))
    )
    """,
    """
    CREATE TABLE finance.account_identifier (
        id uuid PRIMARY KEY,
        account_id uuid NOT NULL REFERENCES finance.account (id),
        identifier text NOT NULL CHECK (identifier <> ''),
        kind text NOT NULL CHECK (kind IN ('number', 'masked_card', 'file_token',
            'portfolio_name', 'internal')),
        valid_from date,
        valid_to date,
        CHECK (valid_to >= valid_from),
        UNIQUE (kind, identifier)
    )
    """,
    """
    CREATE TABLE finance.manual_entry_error (
        id uuid PRIMARY KEY,
        file text NOT NULL CHECK (file <> ''),
        line_no integer NOT NULL CHECK (line_no >= 1),
        reason text NOT NULL CHECK (reason <> ''),
        UNIQUE (file, line_no, reason)
    )
    """,
)


def upgrade() -> None:
    for statement in STATEMENTS:
        op.execute(statement)


def downgrade() -> None:
    raise RuntimeError("finance migrations are forward-only")
