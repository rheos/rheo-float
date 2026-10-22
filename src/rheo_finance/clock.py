"""The injectable clock and local-time helpers (FR-50).

Code that needs "now" takes a ``Clock``; tests pass a ``FakeClock``. Stored timestamps
are UTC. Every user-facing time is rendered, and every local date computed, in the zone
the ``finance.locale.timezone`` setting names (``Settings.timezone``), through zoneinfo:
a zone name, never a fixed offset, so a move to a zone with daylight saving is a
settings change.
"""

from __future__ import annotations

import datetime as dt
from typing import Protocol
from zoneinfo import ZoneInfo


class Clock(Protocol):
    def now_utc(self) -> dt.datetime:
        """The current instant, timezone-aware, in UTC."""
        ...


def as_utc(instant: dt.datetime) -> dt.datetime:
    """``instant`` in UTC. A naive datetime is refused: it names no instant."""
    if instant.tzinfo is None or instant.utcoffset() is None:
        raise ValueError("a naive datetime has no time zone; pass an aware one")
    return instant.astimezone(dt.UTC)


class SystemClock:
    def now_utc(self) -> dt.datetime:
        return dt.datetime.now(dt.UTC)


class FakeClock:
    """A clock that stands still until a test moves it."""

    def __init__(self, start: dt.datetime) -> None:
        self._now = as_utc(start)

    def now_utc(self) -> dt.datetime:
        return self._now

    def set(self, instant: dt.datetime) -> None:
        self._now = as_utc(instant)

    def advance(self, delta: dt.timedelta) -> None:
        self._now += delta


def to_local(instant: dt.datetime, zone: ZoneInfo) -> dt.datetime:
    """``instant`` as wall-clock time in ``zone``."""
    return as_utc(instant).astimezone(zone)


def local_date(instant: dt.datetime, zone: ZoneInfo) -> dt.date:
    """The calendar date in ``zone`` at ``instant``."""
    return to_local(instant, zone).date()


def local_today(clock: Clock, zone: ZoneInfo) -> dt.date:
    return local_date(clock.now_utc(), zone)


def render_local(instant: dt.datetime, zone: ZoneInfo) -> str:
    """``YYYY-MM-DD HH:MM`` in ``zone``, then the zone abbreviation at that instant."""
    return to_local(instant, zone).strftime("%Y-%m-%d %H:%M %Z")


def local_to_utc(day: dt.date, wall: dt.time, zone: ZoneInfo) -> dt.datetime:
    """The UTC instant of wall-clock ``wall`` on ``day`` in ``zone``.

    For scheduled local times (``finance.digest.time`` and the like). On a daylight
    saving gap or overlap, zoneinfo's ``fold=0`` reading applies.
    """
    return dt.datetime.combine(day, wall, tzinfo=zone).astimezone(dt.UTC)
