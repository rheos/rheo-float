"""Safe orphan candidate line references without additional financial rows."""

from alembic import op

revision = "0034_open_item_candidates"
down_revision = "0033_open_item"
branch_labels = None
depends_on = None

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
        candidate_count, CAST((SELECT coalesce(array_agg(DISTINCT concat('finance.txn:',
        finance.decision_orphan.candidate_txn_id) ORDER BY concat('finance.txn:',
        finance.decision_orphan.candidate_txn_id)), ARRAY[]::TEXT[]) AS coalesce_1 FROM
        finance.decision_orphan WHERE finance.decision_orphan.decision_id =
        finance.open_item.decision_id AND finance.decision_orphan.candidate_txn_id IS
        NOT NULL) AS TEXT[]) AS candidate_refs FROM finance.open_item LEFT OUTER JOIN
        finance.account ON finance.open_item.account_id = finance.account.id LEFT OUTER
        JOIN finance.txn ON finance.open_item.txn_id = finance.txn.id LEFT OUTER JOIN
        finance.account AS reason_account ON finance.open_item.reason_account_id =
        reason_account.id LEFT OUTER JOIN finance.payee ON
        finance.open_item.reason_payee_id = finance.payee.id
    """,
}


def upgrade() -> None:
    for name, body in VIEWS.items():
        op.execute(f"CREATE OR REPLACE VIEW finance.{name} AS {body}")
        op.execute(f"ALTER VIEW finance.{name} OWNER TO finance_owner")
        op.execute(f"GRANT SELECT ON finance.{name} TO finance_reader")


def downgrade() -> None:
    raise RuntimeError("finance migrations are forward-only")
