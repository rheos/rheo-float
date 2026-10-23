"""Overlapping documents: ``source_discrepancy``, and a line key may repeat on one txn.

``source_discrepancy`` (derived, stage 3) holds each document line that is neither a
txn nor a source of one: an export line inside a tied-out statement period that
matches no line there (``unmatched_in_statement_window``), or a line whose key already
names a txn another line of its document took (``line_key_taken``). ``status`` counts
the rows. A row names the account, the document and the line; the line's amount and
description stay on ``document_line``.

The CSV and the PDF of one cycle usually print a purchase identically, so the export
line, an additional source, carries the same line key as the statement line that is
its txn's primary source. 0010's unique index over the primary and additional rows
refused that. It is replaced by a plain index and a deferred constraint trigger that
keeps the rule it stood for: a line key names one txn, however many sources of that
txn carry it.

No reader grant: report views grant SELECT in their own revisions.
"""

from alembic import op

revision = "0011_overlap"
down_revision = "0010_txn"
branch_labels = None
depends_on = None

STATEMENTS = (
    """
    CREATE TABLE finance.source_discrepancy (
        id uuid PRIMARY KEY,
        account_id uuid NOT NULL REFERENCES finance.account (id)
            DEFERRABLE INITIALLY DEFERRED,
        document_id uuid NOT NULL REFERENCES finance.document (id),
        document_line_id uuid NOT NULL UNIQUE REFERENCES finance.document_line (id),
        kind text NOT NULL CHECK (kind IN ('unmatched_in_statement_window',
            'line_key_taken'))
    )
    """,
    "DROP INDEX finance.txn_source_line_key",
    """
    CREATE INDEX txn_source_line_key ON finance.txn_source (line_key_id)
        WHERE role IN ('primary', 'additional')
    """,
    """
    CREATE FUNCTION finance.require_one_txn_per_line_key() RETURNS trigger
    LANGUAGE plpgsql AS $$
    BEGIN
        IF (
            SELECT count(DISTINCT txn_id) FROM finance.txn_source
            WHERE line_key_id = NEW.line_key_id AND role IN ('primary', 'additional')
        ) > 1 THEN
            RAISE EXCEPTION 'finance line key % names more than one txn',
                NEW.line_key_id USING ERRCODE = 'unique_violation';
        END IF;
        RETURN NULL;
    END
    $$
    """,
    "REVOKE ALL ON FUNCTION finance.require_one_txn_per_line_key() FROM PUBLIC",
    """
    CREATE CONSTRAINT TRIGGER txn_source_line_key_one_txn
        AFTER INSERT OR UPDATE ON finance.txn_source DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION finance.require_one_txn_per_line_key()
    """,
)


def upgrade() -> None:
    for statement in STATEMENTS:
        op.execute(statement)


def downgrade() -> None:
    raise RuntimeError("finance migrations are forward-only")
