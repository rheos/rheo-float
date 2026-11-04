"""The payments of stage 15 (FR-22, Key decision 18; SPEC Data Models ``payment``).

A ``payment`` is money that left an account once, or a notice of it. Its id derives from
its money line or from its notice, and ``payment_evidence`` names what it was seen in. A
txn or a notice line evidences one payment, which the unique constraints hold.
``drop_evidence_id`` has no foreign key yet: its table arrives with the drop records.

``payment_application`` is what a payment paid. A null obligation is an unapplied
credit, and only an unapplied credit has a ``reason``.
"""

from alembic import op

revision = "0026_stage15"
down_revision = "0025_invoice_amount_due"
branch_labels = None
depends_on = None

STATEMENTS = (
    """
    CREATE TABLE finance.payment (
        id uuid PRIMARY KEY,
        payee_id uuid NOT NULL REFERENCES finance.payee (id)
            DEFERRABLE INITIALLY DEFERRED,
        amount numeric NOT NULL CHECK (amount > 0),
        currency text NOT NULL,
        paid_on date NOT NULL,
        source text NOT NULL CHECK (source IN ('money', 'notice_only')),
        state text NOT NULL CHECK (state IN ('active', 'reversed', 'confirm_only'))
    )
    """,
    """
    CREATE TABLE finance.payment_evidence (
        id uuid PRIMARY KEY,
        payment_id uuid NOT NULL REFERENCES finance.payment (id),
        stream_key text NOT NULL CHECK (stream_key <> ''),
        role text NOT NULL CHECK (role IN ('money', 'joined', 'notice')),
        txn_id uuid UNIQUE REFERENCES finance.txn (id),
        document_line_id uuid UNIQUE REFERENCES finance.document_line (id),
        drop_evidence_id uuid UNIQUE,
        CHECK (num_nonnulls(txn_id, document_line_id, drop_evidence_id) = 1)
    )
    """,
    """
    CREATE TABLE finance.payment_application (
        id uuid PRIMARY KEY,
        payment_id uuid NOT NULL REFERENCES finance.payment (id),
        obligation_id uuid REFERENCES finance.obligation (id)
            DEFERRABLE INITIALLY DEFERRED,
        amount numeric(20,2) NOT NULL CHECK (amount > 0),
        reason text CHECK (reason IN ('no_target', 'excess')),
        basis_ref text CHECK (basis_ref <> ''),
        auto boolean NOT NULL,
        CHECK ((obligation_id IS NULL) = (reason IS NOT NULL))
    )
    """,
)


def upgrade() -> None:
    for statement in STATEMENTS:
        op.execute(statement)


def downgrade() -> None:
    raise RuntimeError("finance migrations are forward-only")
