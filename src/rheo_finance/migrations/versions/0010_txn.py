"""The ledger lines: ``txn`` and ``txn_source`` (derivation stage 3).

``txn`` carries every column of the Data Models entry, including the ones later stages
fill (conversion, pairing, classification); stage 3 sets the identity, date, amount and
description columns and leaves the rest at their defaults. A txn's id is the id of its
primary source's line key (``derived_id`` over the natural key), so a decision names a
line by ``finance.txn:<uuid>`` and it survives a rebuild.

``txn_source`` says where a txn came from: a ``document_line`` or a manual entry
reference, never both and never neither. Its ``line_key_id`` is the key a decision's
line reference resolves through, so a reference stays valid when a provisional line is
replaced by a statement line. A line key belongs to at most one txn (a partial unique
index over the primary and additional rows), a source line feeds at most one txn, and
a txn has exactly one primary source.

A txn with no primary source cannot exist: a deferred constraint trigger checks it at
commit, from both sides (inserting a txn, and deleting or changing its primary row).

Both tables are derived and rebuilt by every derive. The account keys are deferred to
commit, as in the parse cache: stage 2 rewrites ``account`` in the same transaction.
No reader grant: report views grant SELECT in their own revisions.

The prompt numbered this revision 0008; 0008 and 0009 were taken, so it is 0010.

A basis class is ``rule``, ``model-auto`` or ``owner``: SPEC names the third value
after the person, and this repo is public and never carries that name, so the stored
word is ``owner``.

``business_unit`` is free text with no list of values. The unit list lives in the
owner's settings, outside the repo; the later classification stage that copies a
decision's unit onto a txn will check the unit against that list. Nothing checks it
yet.
"""

from alembic import op

revision = "0010_txn"
down_revision = "0009_parse_cache"
branch_labels = None
depends_on = None

STATEMENTS = (
    """
    CREATE TABLE finance.txn (
        id uuid PRIMARY KEY,
        account_id uuid NOT NULL REFERENCES finance.account (id)
            DEFERRABLE INITIALLY DEFERRED,
        posting_date date NOT NULL,
        txn_date date,
        occurred_at timestamptz,
        recognition_date date,
        tax_year integer
            GENERATED ALWAYS AS (EXTRACT(YEAR FROM recognition_date)::integer) STORED,
        amount numeric NOT NULL,
        currency text NOT NULL CHECK (currency <> ''),
        asset_identifier text,
        cad_amount numeric(20, 2),
        cad_source text CHECK (cad_source IN ('issuer', 'boc', 'kraken',
            'kraken_usd_boc', 'coinstats', 'native')),
        rate numeric,
        rate_date date,
        conversion_state text CHECK (conversion_state IN ('native', 'converted',
            'unconverted')),
        description_norm text,
        merchant_key text,
        display_label text,
        counterparty text,
        party_id uuid,
        txid text,
        from_address text,
        to_address text,
        origin text NOT NULL CHECK (origin IN ('document_line', 'manual',
            'document_expense', 'derived_fee', 'trading_export')),
        basis text CHECK (basis IN ('statement', 'verified_export',
            'unverified_export', 'manual')),
        provisional boolean NOT NULL DEFAULT false,
        movement_kind text NOT NULL CHECK (movement_kind IN ('fiat',
            'crypto_transfer', 'crypto_swap_leg', 'crypto_fee', 'crypto_unclassified',
            'trading')),
        transfer_like boolean NOT NULL DEFAULT false,
        hint_account_id uuid REFERENCES finance.account (id)
            DEFERRABLE INITIALLY DEFERRED,
        transfer_state text CHECK (transfer_state IN ('paired', 'ambiguous',
            'unresolved')),
        pair_kind text CHECK (pair_kind IN ('own_transfer', 'own_one_sided',
            'conversion', 'reversal', 'trade', 'loan', 'label')),
        allocation_state text CHECK (allocation_state IN ('whole', 'split',
            'unallocated')),
        exclusion text CHECK (exclusion IN ('placeholder', 'spam')),
        orphan_candidate boolean NOT NULL DEFAULT false,
        category text,
        category_basis_ref text,
        category_basis_class text CHECK (category_basis_class IN ('rule',
            'model-auto', 'owner')),
        tag text CHECK (tag IN ('business', 'personal', 'split', 'untagged')),
        business_unit text,
        business_pct numeric CHECK (business_pct >= 0 AND business_pct <= 100),
        tag_basis_ref text,
        tag_basis_class text CHECK (tag_basis_class IN ('rule', 'model-auto',
            'owner')),
        classification_state text NOT NULL DEFAULT 'unclassified'
            CHECK (classification_state IN ('classified', 'unclassified')),
        fee_type text,
        trade_ref text,
        CHECK (tag IS NULL OR tag NOT IN ('business', 'split')
            OR tag_basis_class IS NOT NULL)
    )
    """,
    "CREATE INDEX txn_account_posting ON finance.txn (account_id, posting_date)",
    """
    CREATE TABLE finance.txn_source (
        txn_id uuid NOT NULL REFERENCES finance.txn (id),
        document_line_id uuid REFERENCES finance.document_line (id),
        manual_ref text CHECK (manual_ref <> ''),
        line_key_id uuid NOT NULL,
        role text NOT NULL CHECK (role IN ('primary', 'additional', 'evidence')),
        CHECK ((document_line_id IS NULL) <> (manual_ref IS NULL))
    )
    """,
    "CREATE INDEX txn_source_txn ON finance.txn_source (txn_id)",
    """
    CREATE UNIQUE INDEX txn_source_one_primary ON finance.txn_source (txn_id)
        WHERE role = 'primary'
    """,
    """
    CREATE UNIQUE INDEX txn_source_line_key ON finance.txn_source (line_key_id)
        WHERE role IN ('primary', 'additional')
    """,
    """
    CREATE UNIQUE INDEX txn_source_document_line ON finance.txn_source
        (document_line_id) WHERE role IN ('primary', 'additional')
    """,
    """
    CREATE UNIQUE INDEX txn_source_manual_ref ON finance.txn_source (manual_ref)
        WHERE role IN ('primary', 'additional')
    """,
    """
    CREATE FUNCTION finance.require_txn_primary_source() RETURNS trigger
    LANGUAGE plpgsql AS $$
    DECLARE
        checked uuid;
    BEGIN
        IF TG_TABLE_NAME = 'txn' THEN
            checked := NEW.id;
        ELSE
            checked := OLD.txn_id;
        END IF;
        IF EXISTS (SELECT 1 FROM finance.txn WHERE id = checked)
            AND NOT EXISTS (
                SELECT 1 FROM finance.txn_source
                WHERE txn_id = checked AND role = 'primary'
            ) THEN
            RAISE EXCEPTION 'finance.txn % has no primary source', checked
                USING ERRCODE = 'check_violation';
        END IF;
        RETURN NULL;
    END
    $$
    """,
    "REVOKE ALL ON FUNCTION finance.require_txn_primary_source() FROM PUBLIC",
    """
    CREATE CONSTRAINT TRIGGER txn_has_primary_source
        AFTER INSERT ON finance.txn DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION finance.require_txn_primary_source()
    """,
    """
    CREATE CONSTRAINT TRIGGER txn_source_keeps_primary
        AFTER UPDATE OR DELETE ON finance.txn_source DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION finance.require_txn_primary_source()
    """,
)


def upgrade() -> None:
    for statement in STATEMENTS:
        op.execute(statement)


def downgrade() -> None:
    raise RuntimeError("finance migrations are forward-only")
