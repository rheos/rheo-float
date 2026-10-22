"""The document registry: ``document`` and ``archive_skip``.

``document`` holds one row per distinct file (by sha256) adopted from the archive or
filed into it (FR-2). The archive walk (``documents.registry``) inserts a row the
first time it sees the bytes; parsers set the status, parser, extraction and period
later. Ids come from ``derived_id`` over the sha256, so a rebuild reproduces them.

``archive_skip`` is derived: the walk deletes and rewrites it on every run. It counts
the files the walk did not register, by issuer directory and reason only. A skipped
file's path is stored nowhere (Privacy and safety mechanics, "Adopted paths carry no
identifier").

No reader grant: report views grant SELECT in their own revisions. The CHECKs repeat
the registry's closed sets, so a value that slipped past the code fails the insert.
"""

from alembic import op

revision = "0007_documents"
down_revision = "0006_filed_years"
branch_labels = None
depends_on = None

STATEMENTS = (
    """
    CREATE TABLE finance.document (
        id uuid PRIMARY KEY,
        path text NOT NULL CHECK (path <> '' AND path !~ '^/'),
        sha256 text NOT NULL UNIQUE CHECK (sha256 ~ '^[0-9a-f]{64}$'),
        kind text NOT NULL CHECK (kind IN ('statement', 'bill', 'export',
            'trading_export', 'holdings_snapshot', 'invoice', 'receipt', 'email',
            'drop_record', 'api_response', 'image', 'cra_slip')),
        issuer text NOT NULL CHECK (issuer <> ''),
        format text NOT NULL CHECK (format ~ '^[a-z0-9]+$'),
        official boolean NOT NULL DEFAULT true,
        status text NOT NULL CHECK (status IN ('filed', 'parsed', 'rejected',
            'unparsed', 'needs_manual_entry', 'superseded')),
        parser_id text,
        parser_version text,
        extraction text NOT NULL DEFAULT 'none' CHECK (extraction IN ('fixed_rule',
            'model_masked', 'manual', 'transcription', 'none')),
        reject_reason text,
        period_start date,
        period_end date,
        message_id text,
        imported_at timestamptz NOT NULL,
        CHECK (period_end >= period_start),
        CHECK ((parser_id IS NULL) = (parser_version IS NULL)),
        CHECK (kind <> 'drop_record'
            OR (status = 'filed' AND extraction = 'transcription'))
    )
    """,
    """
    CREATE TABLE finance.archive_skip (
        directory text NOT NULL CHECK (directory <> ''),
        reason text NOT NULL CHECK (reason IN ('identifier_in_path',
            'unmapped_directory', 'not_a_regular_file', 'not_downloaded')),
        count integer NOT NULL CHECK (count >= 1),
        PRIMARY KEY (directory, reason)
    )
    """,
)


def upgrade() -> None:
    for statement in STATEMENTS:
        op.execute(statement)


def downgrade() -> None:
    raise RuntimeError("finance migrations are forward-only")
