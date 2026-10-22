"""The sink gate (Key decision 7, FR-65): is a sink enabled right now?

A sink is enabled only when all three hold: the setting ``finance.sinks.<sink>.enabled``
is true; the newest unreversed ``sink_approve`` or ``sink_disable`` decision for it is a
``sink_approve``; and that approval's ``spec_digest`` equals the digest of the sink's
current spec, computed here in code from its provider on the owner's unit of work. A
hand-set setting with no approval, an approval with the setting off, and an approval
whose spec has changed since (stale) all read as disabled. A sink with no provider is
disabled.

The setting is read from ``$FINANCE_DATA_DIR/settings.toml`` at each call, so a caller
cannot hand the gate a settings object of its own.

The errors below are what the socket owners (``rheo_finance.http``, the IMAP source)
raise before any socket. Their messages name the sink, never a key or header value.
"""

from __future__ import annotations

from rheo_finance.context import UnitOfWork
from rheo_finance.decision import DecisionKind, effective
from rheo_finance.settings import SINKS, load_settings
from rheo_finance.sinks.spec import current_spec, spec_digest

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
    newest = effective(uow, SINK_DECISIONS, sink)
    if newest is None or newest.kind is not DecisionKind.SINK_APPROVE:
        return None
    digest = spec_digest(spec)
    return digest if newest.spec_digest == digest else None


def is_enabled(uow: UnitOfWork, sink: str) -> bool:
    return enabled_digest(uow, sink) is not None
