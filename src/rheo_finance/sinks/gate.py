"""The sink gate (Key decision 7, FR-65): is a sink enabled right now?

A sink is enabled only when all three hold: the setting ``finance.sinks.<sink>.enabled``
is true; the newest ``sink_approve`` or ``sink_disable`` decision for it, reversed or
not, is an unreversed ``sink_approve``; and that approval's ``spec_digest`` equals the
digest of the sink's current spec, computed here in code from its provider on the
owner's unit of work. A hand-set setting with no approval, an approval with the setting
off, and an approval whose spec has changed since (stale) all read as disabled. A sink
with no provider is disabled.

Reversing the newest approval disables the sink, as a disable does: the gate does not
fall back to an older approval (``decision.effective`` would), so turning the sink on
again takes a fresh approval.

The setting is read from ``$FINANCE_DATA_DIR/settings.toml`` at each call, so a caller
cannot hand the gate a settings object of its own.

The errors below are what the socket owners (``rheo_finance.http``, the IMAP source)
raise before any socket. Their messages name the sink, never a key or header value.
"""

from __future__ import annotations

from sqlalchemy import and_, exists, or_, select

from rheo_finance.context import UnitOfWork
from rheo_finance.decision import DecisionKind
from rheo_finance.settings import SINKS, load_settings
from rheo_finance.sinks.spec import current_spec, spec_digest
from rheo_finance.storage.schema import decision

SINK_DECISIONS = (DecisionKind.SINK_APPROVE, DecisionKind.SINK_DISABLE)


class SinkDisabled(Exception):
    """The sink is not enabled; a job skips and leaves its work pending."""


class HostRefused(Exception):
    """The URL's scheme or host is not one the sink declares, or a redirect left it."""


class AllowlistRefused(Exception):
    """The method, path or query is not on the client's allowlist."""


def enabled_digest(uow: UnitOfWork, sink: str) -> str | None:
    """The approved spec digest when ``sink`` is enabled, else None."""
    if sink not in SINKS:
        return None
    if not load_settings().sink_enabled_setting(sink):
        return None
    spec = current_spec(uow, sink)
    if spec is None:
        return None
    newest = _newest_sink_decision(uow, sink)
    if newest is None:
        return None
    kind, approved_digest, reversed_ = newest
    if kind != DecisionKind.SINK_APPROVE.value or reversed_:
        return None
    digest = spec_digest(spec)
    return digest if approved_digest == digest else None


def _newest_sink_decision(
    uow: UnitOfWork, sink: str
) -> tuple[str, str | None, bool] | None:
    """(kind, spec_digest, reversed) of the newest sink decision for ``sink``,
    reversed or not; newest is by ``decided_at``, then id."""
    rev = decision.alias("rev")
    reversed_ = exists().where(
        rev.c.kind == DecisionKind.REVERSE.value,
        or_(
            rev.c.reverses_id == decision.c.id,
            and_(
                decision.c.bulk_id.is_not(None),
                rev.c.reverses_bulk_id == decision.c.bulk_id,
            ),
        ),
    )
    row = uow.execute(
        select(decision.c.kind, decision.c.spec_digest, reversed_.label("reversed"))
        .where(
            decision.c.kind.in_([kind.value for kind in SINK_DECISIONS]),
            decision.c.sink == sink,
        )
        .order_by(decision.c.decided_at.desc(), decision.c.id.desc())
        .limit(1)
    ).first()
    if row is None:
        return None
    return str(row.kind), row.spec_digest, bool(row.reversed)


def is_enabled(uow: UnitOfWork, sink: str) -> bool:
    return enabled_digest(uow, sink) is not None
