"""Stage 17 open subjects and their restricted-money reader projection."""

from alembic import op

revision = "0033_open_item"
down_revision = "0032_decision_orphan"
branch_labels = None
depends_on = None

STATEMENTS = (
    """
    CREATE TABLE finance.open_item (
        id uuid PRIMARY KEY,
        tax_year integer NOT NULL,
        kind text NOT NULL CHECK (kind IN (
            'unresolved_transfer', 'ambiguous_transfer', 'unconverted',
            'needs_source', 'unmatched_invoice', 'orphaned_decision',
            'unapplied_credit'
        )),
        subject_ref text NOT NULL CHECK (subject_ref <> ''),
        txn_id uuid REFERENCES finance.txn (id),
        document_id uuid REFERENCES finance.document (id),
        decision_id uuid REFERENCES finance.decision (id),
        payment_id uuid REFERENCES finance.payment (id),
        occurred_on date NOT NULL,
        account_id uuid REFERENCES finance.account (id)
            DEFERRABLE INITIALLY DEFERRED,
        direction text CHECK (direction IN ('in', 'out')),
        amount numeric,
        currency text,
        cad_amount numeric(20,2),
        state text NOT NULL,
        reason_code text NOT NULL CHECK (reason_code IN (
            'counterpart_not_loaded', 'counterpart_no_data', 'no_match',
            'candidates', 'no_rate', 'no_source', 'unmatched', 'orphan_candidate',
            'orphan_no_candidate', 'no_target', 'excess'
        )),
        reason_account_id uuid REFERENCES finance.account (id)
            DEFERRABLE INITIALLY DEFERRED,
        reason_date date,
        reason_payee_id uuid REFERENCES finance.payee (id)
            DEFERRABLE INITIALLY DEFERRED,
        category text,
        tag text,
        UNIQUE (kind, subject_ref),
        CHECK (tax_year = EXTRACT(YEAR FROM occurred_on)::integer),
        CHECK ((amount IS NULL) = (direction IS NULL)),
        CHECK (amount IS NULL OR (direction = 'out') = (amount < 0))
    )
    """,
)

VIEWS = {
    "report_open_item": """
        SELECT finance.open_item.subject_ref AS line_ref, finance.open_item.tax_year,
        finance.open_item.kind, finance.open_item.occurred_on, finance.account.label AS
        account_label, finance.open_item.direction, finance.open_item.amount,
        finance.open_item.currency, finance.open_item.cad_amount,
        finance.open_item.state, CASE WHEN (finance.open_item.reason_code =
        'counterpart_no_data') THEN concat(coalesce(reason_account.label,
        'Counterpart'), ' side not loaded: no ', coalesce(reason_account.label,
        'Counterpart'), ' export filed') WHEN (finance.open_item.reason_code =
        'counterpart_not_loaded') THEN CASE WHEN (finance.open_item.reason_date IS NOT
        NULL) THEN concat(coalesce(reason_account.label, 'Counterpart'),
        ' side not loaded: no ', coalesce(reason_account.label, 'Counterpart'),
        ' statement before ', CAST(finance.open_item.reason_date AS TEXT)) ELSE
        concat(coalesce(reason_account.label, 'Counterpart'), ' side not loaded: no ',
        coalesce(reason_account.label, 'Counterpart'), ' data for ',
        to_char(finance.txn.posting_date, 'YYYY-MM')) END WHEN
        (finance.open_item.reason_code = 'candidates') THEN concat(CASE WHEN
        (finance.open_item.kind = 'orphaned_decision') THEN (SELECT count(DISTINCT
        finance.decision_orphan.candidate_txn_id) AS count_1 FROM
        finance.decision_orphan WHERE finance.decision_orphan.decision_id =
        finance.open_item.decision_id AND finance.decision_orphan.candidate_txn_id IS
        NOT NULL) WHEN (finance.open_item.kind = 'ambiguous_transfer') THEN (SELECT
        count(*) AS count_2 FROM finance.pair_edge WHERE (finance.pair_edge.a_txn_id =
        finance.open_item.txn_id OR finance.pair_edge.b_txn_id =
        finance.open_item.txn_id) AND finance.pair_edge.applied IS false) ELSE 0 END,
        ' candidates') WHEN (finance.open_item.reason_code = 'no_match') THEN
        'no matching counterpart' WHEN (finance.open_item.reason_code = 'no_rate') THEN
        'no exchange rate' WHEN (finance.open_item.reason_code = 'no_source') THEN
        'no usable source' WHEN (finance.open_item.reason_code = 'unmatched') THEN
        'invoice has no matching payment' WHEN (finance.open_item.reason_code =
        'orphan_candidate') THEN concat('decision orphaned: ', CASE WHEN
        (finance.open_item.kind = 'orphaned_decision') THEN (SELECT count(DISTINCT
        finance.decision_orphan.candidate_txn_id) AS count_1 FROM
        finance.decision_orphan WHERE finance.decision_orphan.decision_id =
        finance.open_item.decision_id AND finance.decision_orphan.candidate_txn_id IS
        NOT NULL) WHEN (finance.open_item.kind = 'ambiguous_transfer') THEN (SELECT
        count(*) AS count_2 FROM finance.pair_edge WHERE (finance.pair_edge.a_txn_id =
        finance.open_item.txn_id OR finance.pair_edge.b_txn_id =
        finance.open_item.txn_id) AND finance.pair_edge.applied IS false) ELSE 0 END,
        ' replacement candidates') WHEN (finance.open_item.reason_code =
        'orphan_no_candidate') THEN 'decision orphaned: no replacement candidate' WHEN
        (finance.open_item.reason_code = 'excess') THEN
        concat('credit exceeds obligation for ', coalesce(finance.payee.display_label,
        'payee'), CASE WHEN ((SELECT string_agg(finance.obligation.period_label, ', '
        ORDER BY finance.obligation.period_label) AS string_agg_1 FROM
        finance.payment_application JOIN finance.obligation ON
        finance.payment_application.obligation_id = finance.obligation.id WHERE
        finance.payment_application.payment_id = finance.open_item.payment_id) IS NOT
        NULL) THEN concat(' (', (SELECT string_agg(finance.obligation.period_label, ', '
        ORDER BY finance.obligation.period_label) AS string_agg_1 FROM
        finance.payment_application JOIN finance.obligation ON
        finance.payment_application.obligation_id = finance.obligation.id WHERE
        finance.payment_application.payment_id = finance.open_item.payment_id), ')')
        ELSE '' END) WHEN (finance.open_item.reason_code = 'no_target') THEN CASE WHEN
        (finance.open_item.reason_date IS NOT NULL) THEN concat('no target for ',
        coalesce(finance.payee.display_label, 'payee'), ' issued by ',
        CAST(finance.open_item.reason_date AS TEXT)) ELSE
        concat('credit not applied to ', coalesce(finance.payee.display_label, 'payee'))
        END ELSE replace(finance.open_item.reason_code, '_', ' ') END AS reason,
        finance.open_item.category, finance.open_item.tag, CASE WHEN
        (finance.open_item.kind = 'orphaned_decision') THEN (SELECT count(DISTINCT
        finance.decision_orphan.candidate_txn_id) AS count_1 FROM
        finance.decision_orphan WHERE finance.decision_orphan.decision_id =
        finance.open_item.decision_id AND finance.decision_orphan.candidate_txn_id IS
        NOT NULL) WHEN (finance.open_item.kind = 'ambiguous_transfer') THEN (SELECT
        count(*) AS count_2 FROM finance.pair_edge WHERE (finance.pair_edge.a_txn_id =
        finance.open_item.txn_id OR finance.pair_edge.b_txn_id =
        finance.open_item.txn_id) AND finance.pair_edge.applied IS false) ELSE 0 END AS
        candidate_count FROM finance.open_item LEFT OUTER JOIN finance.account ON
        finance.open_item.account_id = finance.account.id LEFT OUTER JOIN finance.txn ON
        finance.open_item.txn_id = finance.txn.id LEFT OUTER JOIN finance.account AS
        reason_account ON finance.open_item.reason_account_id = reason_account.id LEFT
        OUTER JOIN finance.payee ON finance.open_item.reason_payee_id = finance.payee.id
    """,
}


def upgrade() -> None:
    for statement in STATEMENTS:
        op.execute(statement)
    for name, body in VIEWS.items():
        op.execute(f"CREATE VIEW finance.{name} AS {body}")
        op.execute(f"ALTER VIEW finance.{name} OWNER TO finance_owner")
        op.execute(f"GRANT SELECT ON finance.{name} TO finance_reader")


def downgrade() -> None:
    raise RuntimeError("finance migrations are forward-only")
