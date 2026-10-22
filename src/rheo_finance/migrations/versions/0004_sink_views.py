"""The sink report views: ``report_sink_spec`` and ``report_sink``.

Both are owned by ``finance_owner`` (the role that migrates) and the reader gets SELECT
on them in this revision; the reader holds no other privilege.

``VIEWS`` is frozen SQL: the bodies ``rheo_finance.sinks.views`` compiled to when this
revision was written, wrapped at its spaces. ``report_sink`` states the gate's rule
with the gate's own ``reversed_clause``: the newest sink decision, reversed or not,
counts only as an unreversed approval. A guard test compares what the code compiles to
now with the newest revision's ``VIEWS`` entry for each view (whitespace aside), so a
rule changed in code fails until a new revision recreates the view.
"""

from alembic import op

revision = "0004_sink_views"
down_revision = "0003_sink_spec"
branch_labels = None
depends_on = None

VIEWS = {
    "report_sink_spec": """
        SELECT finance.sink_spec.sink, finance.sink_spec.spec_digest,
        finance.sink_spec.hosts, finance.sink_spec.request_templates,
        finance.sink_spec.key_file, finance.sink_spec.policy_version,
        finance.sink_spec.data_inputs
        FROM finance.sink_spec
    """,
    "report_sink": """
        SELECT coalesce(finance.sink_spec.sink, newest.sink) AS sink,
        CASE WHEN (newest.kind = 'sink_approve' AND NOT newest.reversed)
            THEN newest.decided_at END AS approved_at,
        CASE WHEN (newest.kind = 'sink_approve' AND NOT newest.reversed)
            THEN newest.spec_digest END AS spec_digest,
        coalesce(newest.kind = 'sink_approve' AND NOT newest.reversed
            AND newest.spec_digest IS DISTINCT FROM finance.sink_spec.spec_digest,
            false) AS stale
        FROM finance.sink_spec FULL OUTER JOIN (SELECT DISTINCT ON
            (finance.decision.sink) finance.decision.sink AS sink,
            finance.decision.kind AS kind,
            finance.decision.spec_digest AS spec_digest,
            finance.decision.decided_at AS decided_at,
            EXISTS (SELECT *
                FROM finance.decision AS rev
                WHERE rev.kind = 'reverse' AND (rev.reverses_id = finance.decision.id
                OR finance.decision.bulk_id IS NOT NULL
                AND rev.reverses_bulk_id = finance.decision.bulk_id)) AS reversed
            FROM finance.decision
            WHERE finance.decision.kind IN ('sink_approve', 'sink_disable')
            ORDER BY finance.decision.sink, finance.decision.decided_at DESC,
            finance.decision.id DESC) AS newest
        ON newest.sink = finance.sink_spec.sink
    """,
}


def statements() -> list[str]:
    out = [f"CREATE VIEW finance.{name} AS {body}" for name, body in VIEWS.items()]
    out.append(
        "GRANT SELECT ON finance.report_sink_spec, finance.report_sink"
        " TO finance_reader"
    )
    return out


def upgrade() -> None:
    for statement in statements():
        op.execute(statement)


def downgrade() -> None:
    raise RuntimeError("finance migrations are forward-only")
