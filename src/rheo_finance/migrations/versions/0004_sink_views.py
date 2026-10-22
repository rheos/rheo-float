"""The sink report views: ``report_sink_spec`` and ``report_sink``.

Both are owned by ``finance_owner`` (the role that migrates) and the reader gets SELECT
on them in this revision; the reader holds no other privilege. The view bodies are
compiled from ``rheo_finance.sinks.views``, so ``report_sink`` states "newest sink
decision, reversed or not; only an unreversed approval counts" with the gate's own
``reversed_clause``.
"""

from alembic import op

from rheo_finance.sinks.views import (
    report_sink_select,
    report_sink_spec_select,
    view_sql,
)

revision = "0004_sink_views"
down_revision = "0003_sink_spec"
branch_labels = None
depends_on = None


def statements() -> list[str]:
    return [
        "CREATE VIEW finance.report_sink_spec AS "
        + view_sql(report_sink_spec_select()),
        "CREATE VIEW finance.report_sink AS " + view_sql(report_sink_select()),
        "GRANT SELECT ON finance.report_sink_spec, finance.report_sink"
        " TO finance_reader",
    ]


def upgrade() -> None:
    for statement in statements():
        op.execute(statement)


def downgrade() -> None:
    raise RuntimeError("finance migrations are forward-only")
