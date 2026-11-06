"""Owed reader facts, evidence, credit identities and bill-gap counts."""

from alembic import op

revision = "0035_owed"
down_revision = "0034_open_item_candidates"
branch_labels = None
depends_on = None

VIEWS = {
    "report_document": """
        SELECT finance.document.path, finance.document.kind, finance.document.status,
        finance.document.period_start, finance.document.period_end,
        finance.document.official, finance.document.extraction,
        finance.document.issuer, EXISTS (SELECT finance.obligation.id FROM
        finance.obligation WHERE finance.obligation.primary_source =
        finance.document.path) AS has_obligation FROM finance.document
    """,
    "report_status": """
        SELECT facts.fact, facts.subject, facts.count, facts.at FROM (SELECT 'schema'
        AS fact, finance.alembic_version_finance.version_num AS subject, CAST(1 AS
        BIGINT) AS count, CAST(NULL AS TIMESTAMP WITH TIME ZONE) AS at FROM
        finance.alembic_version_finance UNION ALL SELECT 'decision_orphan' AS fact,
        CAST(NULL AS TEXT) AS subject, count(DISTINCT
        finance.decision_orphan.decision_id) AS count, CAST(NULL AS TIMESTAMP WITH
        TIME ZONE) AS at FROM finance.decision_orphan UNION ALL SELECT 'bill_gap' AS
        fact, finance.document.issuer AS subject, count(*) AS count, CAST(NULL AS
        TIMESTAMP WITH TIME ZONE) AS at FROM finance.document WHERE
        finance.document.kind IN ('bill', 'image') AND finance.document.status IN
        ('needs_manual_entry', 'unparsed') AND NOT (EXISTS (SELECT
        finance.obligation.id FROM finance.obligation WHERE
        finance.obligation.primary_source = finance.document.path)) GROUP BY
        finance.document.issuer UNION ALL SELECT 'last_import' AS fact,
        split_part(finance.document.issuer, '/', 1) AS subject, count(*) AS count,
        max(finance.document.imported_at) AS at FROM finance.document GROUP BY
        split_part(finance.document.issuer, '/', 1) UNION ALL SELECT 'document_status'
        AS fact, finance.document.status AS subject, count(*) AS count, CAST(NULL AS
        TIMESTAMP WITH TIME ZONE) AS at FROM finance.document GROUP BY
        finance.document.status UNION ALL SELECT 'source_discrepancy' AS fact,
        finance.source_discrepancy.kind AS subject, count(*) AS count, CAST(NULL AS
        TIMESTAMP WITH TIME ZONE) AS at FROM finance.source_discrepancy GROUP BY
        finance.source_discrepancy.kind UNION ALL SELECT 'archive_skip_identifier' AS
        fact, finance.archive_skip.directory AS subject,
        CAST(finance.archive_skip.count AS BIGINT) AS count, CAST(NULL AS TIMESTAMP
        WITH TIME ZONE) AS at FROM finance.archive_skip WHERE
        finance.archive_skip.reason = 'identifier_in_path' UNION ALL SELECT
        'account_no_data' AS fact, finance.account.label AS subject, CAST(1 AS BIGINT)
        AS count, CAST(NULL AS TIMESTAMP WITH TIME ZONE) AS at FROM finance.account
        WHERE NOT (EXISTS (SELECT finance.coverage.account_id FROM finance.coverage
        WHERE finance.coverage.account_id = finance.account.id)) AND
        finance.account.slug != 'paid-outside' UNION ALL SELECT 'missing_months' AS
        fact, finance.account.label AS subject, count(*) AS count, CAST(NULL AS
        TIMESTAMP WITH TIME ZONE) AS at FROM finance.coverage JOIN finance.account ON
        finance.coverage.account_id = finance.account.id WHERE finance.coverage.state
        = 'missing' GROUP BY finance.account.id, finance.account.label UNION ALL
        SELECT 'duplicate_bill_month' AS fact, finance.payee.display_label AS subject,
        count(*) AS count, CAST(NULL AS TIMESTAMP WITH TIME ZONE) AS at FROM
        finance.obligation JOIN finance.payee ON finance.obligation.payee_id =
        finance.payee.id WHERE finance.obligation.kind = 'bill' AND
        finance.obligation.period_label ~ '^bill-[0-9]{4}-[0-9]{2}-' GROUP BY
        finance.payee.id, finance.payee.display_label UNION ALL SELECT
        'manual_mismatch' AS fact, finance.payee.display_label AS subject, count(*) AS
        count, CAST(NULL AS TIMESTAMP WITH TIME ZONE) AS at FROM finance.obligation
        JOIN finance.payee ON finance.obligation.payee_id = finance.payee.id WHERE
        finance.obligation.manual_mismatch IS NOT NULL GROUP BY finance.payee.id,
        finance.payee.display_label UNION ALL SELECT 'statement_conflict' AS fact,
        finance.payee.display_label AS subject, count(*) AS count, CAST(NULL AS
        TIMESTAMP WITH TIME ZONE) AS at FROM finance.obligation JOIN finance.payee ON
        finance.obligation.payee_id = finance.payee.id WHERE
        finance.obligation.statement_conflict IS true GROUP BY finance.payee.id,
        finance.payee.display_label UNION ALL SELECT 'typed_bill_unjoined' AS fact,
        finance.payee.display_label AS subject, count(*) AS count, CAST(NULL AS
        TIMESTAMP WITH TIME ZONE) AS at FROM finance.obligation JOIN finance.payee ON
        finance.obligation.payee_id = finance.payee.id WHERE
        finance.obligation.unjoined IS true GROUP BY finance.payee.id,
        finance.payee.display_label UNION ALL SELECT 'arrangement_unsettled' AS fact,
        finance.obligation.arrangement_ref AS subject, CAST(1 AS BIGINT) AS count,
        CAST(NULL AS TIMESTAMP WITH TIME ZONE) AS at FROM finance.obligation WHERE
        finance.obligation.arrangement_unsettled IS true GROUP BY
        finance.obligation.arrangement_ref UNION ALL SELECT 'arrangement_unsettled_by'
        AS fact, concat(finance.obligation.arrangement_ref, ' finance.decision:',
        finance.obligation.unsettled_decision_id) AS subject, CAST(1 AS BIGINT) AS
        count, CAST(NULL AS TIMESTAMP WITH TIME ZONE) AS at FROM finance.obligation
        WHERE finance.obligation.unsettled_decision_id IS NOT NULL GROUP BY
        finance.obligation.arrangement_ref, finance.obligation.unsettled_decision_id
        UNION ALL SELECT 'invoice_decision_superseded' AS fact, finance.decision.kind
        AS subject, count(*) AS count, CAST(NULL AS TIMESTAMP WITH TIME ZONE) AS at
        FROM finance.txn_source JOIN finance.decision ON
        finance.txn_source.overrides_decision_id = finance.decision.id GROUP BY
        finance.decision.kind UNION ALL SELECT 'invoice_decision_refused' AS fact,
        finance.decision.kind AS subject, count(*) AS count, CAST(NULL AS TIMESTAMP
        WITH TIME ZONE) AS at FROM finance.document_line JOIN finance.decision ON
        finance.document_line.refused_decision_id = finance.decision.id GROUP BY
        finance.decision.kind) AS facts
    """,
    "report_open_item": """
        SELECT finance.open_item.subject_ref AS line_ref, finance.open_item.tax_year,
        finance.open_item.kind, finance.open_item.occurred_on, finance.account.label
        AS account_label, finance.open_item.direction, finance.open_item.amount,
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
        'no matching counterpart' WHEN (finance.open_item.reason_code = 'no_rate')
        THEN 'no exchange rate' WHEN (finance.open_item.reason_code = 'no_source')
        THEN 'no usable source' WHEN (finance.open_item.reason_code = 'unmatched')
        THEN 'invoice has no matching payment' WHEN (finance.open_item.reason_code =
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
        NULL) THEN concat(' (', (SELECT string_agg(finance.obligation.period_label,
        ', ' ORDER BY finance.obligation.period_label) AS string_agg_1 FROM
        finance.payment_application JOIN finance.obligation ON
        finance.payment_application.obligation_id = finance.obligation.id WHERE
        finance.payment_application.payment_id = finance.open_item.payment_id), ')')
        ELSE '' END) WHEN (finance.open_item.reason_code = 'no_target') THEN CASE WHEN
        (finance.open_item.reason_date IS NOT NULL) THEN concat('no target for ',
        coalesce(finance.payee.display_label, 'payee'), ' issued by ',
        CAST(finance.open_item.reason_date AS TEXT)) ELSE
        concat('credit not applied to ', coalesce(finance.payee.display_label,
        'payee')) END ELSE replace(finance.open_item.reason_code, '_', ' ') END AS
        reason, finance.open_item.category, finance.open_item.tag, CASE WHEN
        (finance.open_item.kind = 'orphaned_decision') THEN (SELECT count(DISTINCT
        finance.decision_orphan.candidate_txn_id) AS count_1 FROM
        finance.decision_orphan WHERE finance.decision_orphan.decision_id =
        finance.open_item.decision_id AND finance.decision_orphan.candidate_txn_id IS
        NOT NULL) WHEN (finance.open_item.kind = 'ambiguous_transfer') THEN (SELECT
        count(*) AS count_2 FROM finance.pair_edge WHERE (finance.pair_edge.a_txn_id =
        finance.open_item.txn_id OR finance.pair_edge.b_txn_id =
        finance.open_item.txn_id) AND finance.pair_edge.applied IS false) ELSE 0 END
        AS candidate_count, CAST((SELECT coalesce(array_agg(DISTINCT
        concat('finance.txn:', finance.decision_orphan.candidate_txn_id) ORDER BY
        concat('finance.txn:', finance.decision_orphan.candidate_txn_id)),
        ARRAY[]::TEXT[]) AS coalesce_1 FROM finance.decision_orphan WHERE
        finance.decision_orphan.decision_id = finance.open_item.decision_id AND
        finance.decision_orphan.candidate_txn_id IS NOT NULL) AS TEXT[]) AS
        candidate_refs, nullif(concat('finance.payment:',
        finance.open_item.payment_id), 'finance.payment:') AS payment_ref,
        finance.payee.slug AS payee_slug, finance.payee.display_label AS payee_label
        FROM finance.open_item LEFT OUTER JOIN finance.account ON
        finance.open_item.account_id = finance.account.id LEFT OUTER JOIN finance.txn
        ON finance.open_item.txn_id = finance.txn.id LEFT OUTER JOIN finance.account
        AS reason_account ON finance.open_item.reason_account_id = reason_account.id
        LEFT OUTER JOIN finance.payee ON finance.open_item.reason_payee_id =
        finance.payee.id
    """,
    "report_owed": """
        SELECT concat('finance.obligation:', finance.obligation.id) AS obligation_ref,
        finance.payee.slug AS payee_slug, finance.payee.display_label AS payee_label,
        finance.obligation.kind, finance.obligation.period_label,
        finance.obligation.statement_date, finance.obligation.issued_on,
        finance.obligation.amount_due, finance.obligation.minimum_due,
        finance.obligation.threshold_due, finance.obligation.paid,
        finance.obligation.remaining, finance.obligation.threshold_remaining,
        finance.obligation.due_date, finance.obligation.status,
        finance.obligation.primary_source, finance.obligation.manual_source,
        finance.obligation.before_floor, cardinality(finance.payee.patterns) = 0 AS
        payments_unmatchable, finance.obligation.manual_mismatch,
        finance.obligation.payments_mismatch, finance.obligation.statement_conflict,
        finance.obligation.invoice_ref, finance.obligation.arrangement_ref,
        nullif(concat('finance.obligation:', finance.obligation.supersedes_id),
        'finance.obligation:') AS supersedes_ref, nullif(concat('finance.decision:',
        finance.obligation.presumed_paid_decision_id), 'finance.decision:') AS
        presumed_paid_basis FROM finance.obligation JOIN finance.payee ON
        finance.obligation.payee_id = finance.payee.id WHERE finance.obligation.status
        IN ('open', 'partial', 'presumed_paid', 'disputed')
    """,
    "report_owed_trace": """
        SELECT concat('finance.obligation:', finance.obligation.id) AS obligation_ref,
        finance.payee.slug AS payee_slug, finance.payee.display_label AS payee_label,
        finance.obligation.kind, finance.obligation.period_label,
        finance.obligation.statement_date, finance.obligation.issued_on,
        finance.obligation.amount_due, finance.obligation.minimum_due,
        finance.obligation.threshold_due, finance.obligation.paid,
        finance.obligation.remaining, finance.obligation.threshold_remaining,
        finance.obligation.due_date, finance.obligation.status,
        finance.obligation.primary_source, finance.obligation.manual_source,
        finance.obligation.before_floor, cardinality(finance.payee.patterns) = 0 AS
        payments_unmatchable, finance.obligation.manual_mismatch,
        finance.obligation.payments_mismatch, finance.obligation.statement_conflict,
        finance.obligation.invoice_ref, finance.obligation.arrangement_ref,
        nullif(concat('finance.obligation:', finance.obligation.supersedes_id),
        'finance.obligation:') AS supersedes_ref, nullif(concat('finance.decision:',
        finance.obligation.presumed_paid_decision_id), 'finance.decision:') AS
        presumed_paid_basis, nullif(concat('finance.payment_application:',
        finance.payment_application.id), 'finance.payment_application:') AS
        application_ref, nullif(concat('finance.payment:', finance.payment.id),
        'finance.payment:') AS payment_ref, finance.payment_application.amount AS
        applied_amount, finance.payment_application.basis_ref AS application_basis,
        finance.payment_application.auto AS application_auto, finance.payment.amount
        AS payment_amount, finance.payment.currency, finance.payment.paid_on,
        finance.payment.source AS payment_source, finance.payment.state AS
        payment_state, nullif(concat('finance.payment_evidence:',
        finance.payment_evidence.id), 'finance.payment_evidence:') AS evidence_ref,
        finance.payment_evidence.role AS evidence_role, nullif(concat('finance.txn:',
        finance.payment_evidence.txn_id), 'finance.txn:') AS txn_ref,
        finance.document.path AS evidence_document, finance.document_line.line_no,
        nullif(concat('finance.drop_evidence:',
        finance.payment_evidence.drop_evidence_id), 'finance.drop_evidence:') AS
        drop_ref, finance.decision.kind AS presumed_basis_kind,
        finance.decision.decided_at AS presumed_at FROM finance.obligation JOIN
        finance.payee ON finance.obligation.payee_id = finance.payee.id LEFT OUTER
        JOIN finance.payment_application ON finance.payment_application.obligation_id
        = finance.obligation.id LEFT OUTER JOIN finance.payment ON finance.payment.id
        = finance.payment_application.payment_id LEFT OUTER JOIN
        finance.payment_evidence ON finance.payment_evidence.payment_id =
        finance.payment.id LEFT OUTER JOIN finance.document_line ON
        finance.document_line.id = finance.payment_evidence.document_line_id LEFT
        OUTER JOIN finance.document ON finance.document.id =
        finance.document_line.document_id LEFT OUTER JOIN finance.decision ON
        finance.decision.id = finance.obligation.presumed_paid_decision_id
    """,
}


def upgrade() -> None:
    for name, body in VIEWS.items():
        op.execute(f"CREATE OR REPLACE VIEW finance.{name} AS {body}")
        op.execute(f"ALTER VIEW finance.{name} OWNER TO finance_owner")
        op.execute(f"GRANT SELECT ON finance.{name} TO finance_reader")


def downgrade() -> None:
    raise RuntimeError("finance migrations are forward-only")
