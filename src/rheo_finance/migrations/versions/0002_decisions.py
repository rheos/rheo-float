"""The decision tables: ``decision``, ``decision_item`` and ``decision_target``.

All three are append-only: a trigger refuses UPDATE, DELETE and TRUNCATE (FR-18).
``decision`` carries one CHECK per kind, named ``decision_<kind>_columns``, saying which
columns that kind may and must use. The matrix below is a frozen copy of
``rheo_finance.decision.DECISION_KIND_COLUMNS`` as of this revision; a later revision
that widens a kind drops and re-adds that kind's constraint, and the decision tests
fail while the database and the code matrix disagree.
"""

from alembic import op

revision = "0002_decisions"
down_revision = "0001_finance_schema"
branch_labels = None
depends_on = None

KINDS = (
    "category", "pair", "unpair", "split", "proposal_decline", "include",
    "payment_apply", "payment_unapply", "obligation_dispute", "presumed_paid",
    "invoice_paid", "invoice_unpaid", "finding_dismiss", "sender_approve",
    "sink_approve", "sink_disable", "reattach_decline", "reverse",
)  # fmt: skip

# The columns a kind's CHECK governs. id, kind, note and decided_at are every kind's.
MATRIX_COLUMNS = (
    "target_ref", "merchant_key", "category", "tag", "business_unit", "business_pct",
    "counterpart_ref", "obligation_ref", "party_ref", "account_ref", "effective_on",
    "sink", "spec_digest", "preview_digest", "amount", "about_decision_id", "bulk_id",
    "reverses_id", "reverses_bulk_id",
)  # fmt: skip

# kind: (required, optional, exactly one of each group, at least one of each group)
_Groups = tuple[tuple[str, ...], ...]
MATRIX: dict[str, tuple[tuple[str, ...], tuple[str, ...], _Groups, _Groups]] = {
    "category": (
        (),
        ("category", "tag", "business_unit", "business_pct", "about_decision_id",
         "bulk_id"),
        (("target_ref", "merchant_key"),),
        (("category", "tag"),),
    ),
    "pair": (
        ("target_ref",),
        ("counterpart_ref", "party_ref", "about_decision_id", "bulk_id"),
        (), (),
    ),
    "unpair": (
        ("target_ref",), ("counterpart_ref", "about_decision_id", "bulk_id"), (), (),
    ),
    "split": (("target_ref",), ("about_decision_id", "bulk_id"), (), ()),
    "proposal_decline": (("target_ref",), ("bulk_id",), (), ()),
    "include": (("target_ref",), ("about_decision_id", "bulk_id"), (), ()),
    "payment_apply": (
        ("target_ref", "obligation_ref"),
        ("amount", "about_decision_id", "bulk_id"),
        (), (),
    ),
    "payment_unapply": (
        ("target_ref",), ("obligation_ref", "about_decision_id", "bulk_id"), (), (),
    ),
    "obligation_dispute": (
        ("obligation_ref",), ("about_decision_id", "bulk_id"), (), (),
    ),
    "presumed_paid": (("obligation_ref",), ("about_decision_id", "bulk_id"), (), ()),
    "invoice_paid": (
        ("target_ref", "account_ref", "effective_on"), ("bulk_id",), (), (),
    ),
    "invoice_unpaid": (("target_ref",), ("effective_on", "bulk_id"), (), ()),
    "finding_dismiss": (("target_ref",), ("amount", "bulk_id"), (), ()),
    "sender_approve": (("target_ref",), ("bulk_id",), (), ()),
    "sink_approve": (("sink", "spec_digest", "preview_digest"), (), (), ()),
    "sink_disable": (("sink",), (), (), ()),
    "reattach_decline": (("about_decision_id",), ("target_ref", "bulk_id"), (), ()),
    "reverse": ((), (), (("reverses_id", "reverses_bulk_id"),), ()),
}  # fmt: skip

APPEND_ONLY_TABLES = ("decision", "decision_item", "decision_target")


def _literal_list(values: tuple[str, ...]) -> str:
    return ", ".join(f"'{value}'" for value in values)


def kind_check(kind: str) -> str:
    """The CHECK expression of one kind, as this revision states it."""
    required, optional, exactly_one, at_least_one = MATRIX[kind]
    allowed = set(required) | set(optional)
    for group in exactly_one + at_least_one:
        allowed |= set(group)
    terms = [f"{column} IS NOT NULL" for column in required]
    terms += [f"{column} IS NULL" for column in MATRIX_COLUMNS if column not in allowed]
    terms += [f"num_nonnulls({', '.join(group)}) = 1" for group in exactly_one]
    terms += [f"num_nonnulls({', '.join(group)}) >= 1" for group in at_least_one]
    body = " AND ".join(terms) if terms else "true"
    return f"kind <> '{kind}' OR ({body})"


def statements() -> list[str]:
    out = [
        f"""
        CREATE TABLE finance.decision (
            id uuid PRIMARY KEY,
            kind text NOT NULL,
            target_ref text,
            merchant_key text,
            category text,
            tag text,
            business_unit text,
            business_pct numeric,
            counterpart_ref text,
            obligation_ref text,
            party_ref text,
            account_ref text,
            effective_on date,
            sink text,
            spec_digest text,
            preview_digest text,
            amount numeric,
            about_decision_id uuid REFERENCES finance.decision (id),
            bulk_id uuid,
            reverses_id uuid REFERENCES finance.decision (id),
            reverses_bulk_id uuid,
            note text,
            decided_at timestamptz NOT NULL,
            CONSTRAINT decision_kind_known CHECK (kind IN ({_literal_list(KINDS)})),
            CONSTRAINT decision_business_pct_range
                CHECK (business_pct >= 0 AND business_pct <= 100)
        )
        """,
        # One reversal per decision and per bulk run.
        "CREATE UNIQUE INDEX decision_reverses_id_once ON finance.decision"
        " (reverses_id) WHERE reverses_id IS NOT NULL",
        "CREATE UNIQUE INDEX decision_reverses_bulk_id_once ON finance.decision"
        " (reverses_bulk_id) WHERE reverses_bulk_id IS NOT NULL",
        """
        CREATE TABLE finance.decision_item (
            decision_id uuid NOT NULL REFERENCES finance.decision (id),
            seq integer NOT NULL CHECK (seq >= 1),
            role text NOT NULL CHECK (role IN ('part', 'leg')),
            target_ref text,
            label text,
            amount numeric,
            covers_ref text,
            PRIMARY KEY (decision_id, seq),
            CONSTRAINT decision_item_part_columns CHECK (
                role <> 'part'
                OR (label IS NOT NULL AND amount IS NOT NULL AND target_ref IS NULL)
            ),
            CONSTRAINT decision_item_leg_columns CHECK (
                role <> 'leg'
                OR (target_ref IS NOT NULL AND label IS NULL AND covers_ref IS NULL)
            )
        )
        """,
        """
        CREATE TABLE finance.decision_target (
            decision_id uuid NOT NULL REFERENCES finance.decision (id),
            seq integer NOT NULL CHECK (seq >= 1),
            target_ref text NOT NULL,
            account_slug text NOT NULL,
            amount numeric NOT NULL,
            currency text NOT NULL,
            posting_date date NOT NULL,
            merchant_key text,
            PRIMARY KEY (decision_id, seq)
        )
        """,
        """
        CREATE FUNCTION finance.refuse_append_only_change() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'finance.% is append-only: % refused',
                TG_TABLE_NAME, TG_OP;
        END
        $$
        """,
        "REVOKE ALL ON FUNCTION finance.refuse_append_only_change() FROM PUBLIC",
    ]
    for kind in KINDS:
        out.append(
            f"ALTER TABLE finance.decision ADD CONSTRAINT decision_{kind}_columns"
            f" CHECK ({kind_check(kind)})"
        )
    for table in APPEND_ONLY_TABLES:
        out.append(
            f"CREATE TRIGGER {table}_append_only BEFORE UPDATE OR DELETE"
            f" ON finance.{table} FOR EACH ROW"
            " EXECUTE FUNCTION finance.refuse_append_only_change()"
        )
        out.append(
            f"CREATE TRIGGER {table}_no_truncate BEFORE TRUNCATE"
            f" ON finance.{table} FOR EACH STATEMENT"
            " EXECUTE FUNCTION finance.refuse_append_only_change()"
        )
    return out


def upgrade() -> None:
    for statement in statements():
        op.execute(statement)


def downgrade() -> None:
    raise RuntimeError("finance migrations are forward-only")
