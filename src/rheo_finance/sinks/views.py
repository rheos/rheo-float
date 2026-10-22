"""The two sink report views, as SQLAlchemy selects that migration 0004 compiles.

``report_sink_spec`` is the derived ``sink_spec`` table without its ``computed_at``:
public text only (hosts, request templates, key file name, policy version, data
inputs), never a sender, key or identifier.

``report_sink`` has one row per sink that has a spec row or a sink decision. It reads
the newest ``sink_approve`` or ``sink_disable`` decision for the sink, reversed or not,
by the gate's own rule (``gate.SINK_DECISIONS`` and ``decision.reversed_clause``): only
an unreversed approval counts, and a reversed newest approval never falls back to an
older one. For that approval it shows ``approved_at`` and the approval's
``spec_digest``; both are null when there is none. ``stale`` is true when there is an
approval and its digest differs from the sink's ``sink_spec.spec_digest`` (a missing
spec row counts as differing), so the reader can tell a stale approval from views
alone. The ``enabled`` setting lives in a file, so the handler combines it.

The views are compiled from these selects when the migration runs. A later change to
``reversed_clause`` or to the rule here needs a revision that recreates the view; the
sink operation tests compare the view with the gate case by case, so they fail while
the two disagree.
"""

from __future__ import annotations

from sqlalchemy import Select, and_, case, false, func, not_, select
from sqlalchemy.dialects.postgresql import distinct_on
from sqlalchemy.dialects.postgresql.base import PGDialect

from rheo_finance.decision import DecisionKind, reversed_clause
from rheo_finance.sinks.gate import SINK_DECISIONS
from rheo_finance.storage.schema import decision, sink_spec


def report_sink_spec_select() -> Select[tuple[object, ...]]:
    return select(
        sink_spec.c.sink,
        sink_spec.c.spec_digest,
        sink_spec.c.hosts,
        sink_spec.c.request_templates,
        sink_spec.c.key_file,
        sink_spec.c.policy_version,
        sink_spec.c.data_inputs,
    )


def report_sink_select() -> Select[tuple[object, ...]]:
    newest = (
        select(
            decision.c.sink,
            decision.c.kind,
            decision.c.spec_digest,
            decision.c.decided_at,
            reversed_clause(decision).label("reversed"),
        )
        .where(decision.c.kind.in_([kind.value for kind in SINK_DECISIONS]))
        .ext(distinct_on(decision.c.sink))
        .order_by(decision.c.sink, decision.c.decided_at.desc(), decision.c.id.desc())
        .subquery("newest")
    )
    approved = and_(
        newest.c.kind == DecisionKind.SINK_APPROVE.value, not_(newest.c.reversed)
    )
    return select(
        func.coalesce(sink_spec.c.sink, newest.c.sink).label("sink"),
        case((approved, newest.c.decided_at)).label("approved_at"),
        case((approved, newest.c.spec_digest)).label("spec_digest"),
        func.coalesce(
            and_(
                approved, newest.c.spec_digest.is_distinct_from(sink_spec.c.spec_digest)
            ),
            false(),
        ).label("stale"),
    ).select_from(sink_spec.join(newest, newest.c.sink == sink_spec.c.sink, full=True))


def view_sql(statement: Select[tuple[object, ...]]) -> str:
    """``statement`` as Postgres SQL with its literals inlined, for ``CREATE VIEW``."""
    # PGDialect's __init__ has no annotations in SQLAlchemy's stubs; it takes no
    # arguments here and returns the dialect, so the call is safe.
    dialect = PGDialect()  # type: ignore[no-untyped-call]
    return str(
        statement.compile(dialect=dialect, compile_kwargs={"literal_binds": True})
    )
