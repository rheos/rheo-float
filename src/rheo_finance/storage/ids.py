"""Record ids (FR-53, Key decision 5).

State rows get an ordinary UUIDv7 from ``uuid7()``. Derived rows get ``derived_id()``:
the same layout, but every bit is a pure function of the row's business date and
natural key, so a rebuild reproduces every id (FR-5) and decisions keep pointing at
their rows (FR-18).

Layout (RFC 9562): 48-bit Unix time in milliseconds, 4 version bits (7), 12 bits, 2
variant bits (0b10), 62 bits. ``uuid7`` fills the 74 free bits at random; ``derived_id``
fills them from sha256 of the canonical natural key.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import secrets
import unicodedata
import uuid
from decimal import Context, Decimal

from rheo_finance.clock import as_utc

_EPOCH = dt.datetime(1970, 1, 1, tzinfo=dt.UTC)
_MS = dt.timedelta(milliseconds=1)
_TIME_BITS = 48
_RAND_BITS = 80
_VERSION_MASK = 0xF << 76
_VARIANT_MASK = 0x3 << 62


def _layout(unix_ms: int, rand: int) -> uuid.UUID:
    if not 0 <= unix_ms < 1 << _TIME_BITS:
        raise ValueError("a UUIDv7 time field holds instants from 1970-01-01 UTC on")
    value = (unix_ms << _RAND_BITS) | (rand & ((1 << _RAND_BITS) - 1))
    value = (value & ~_VERSION_MASK) | (0x7 << 76)
    value = (value & ~_VARIANT_MASK) | (0x2 << 62)
    return uuid.UUID(int=value)


def uuid7(now: dt.datetime | None = None) -> uuid.UUID:
    """A fresh UUIDv7 for a state row, timed at ``now`` (aware) or the system time."""
    instant = dt.datetime.now(dt.UTC) if now is None else as_utc(now)
    return _layout((instant - _EPOCH) // _MS, secrets.randbits(_RAND_BITS))


def _canonical_decimal(value: Decimal) -> str:
    if not value.is_finite():
        raise ValueError("a natural key cannot hold a NaN or infinite Decimal")
    if value.is_zero():
        # 0, 0E-10 and -0 are one zero.
        return "0"
    # normalize() rounds to its context's precision, and the default 28 digits would
    # merge two long crypto quantities into one id; size the context to the digits.
    exact = Context(prec=max(len(value.as_tuple().digits), 1))
    return format(value.normalize(exact), "f")


def _canonical_part(value: object) -> list[object]:
    """One natural-key part as a tagged JSON pair, so types and boundaries hold."""
    if value is None:
        return ["none", None]
    if isinstance(value, bool):
        return ["bool", value]
    if isinstance(value, int | Decimal):
        # One numeric tag: 3 and Decimal("3") are one value, so one id.
        return ["num", _canonical_decimal(Decimal(value))]
    if isinstance(value, str):
        return ["str", unicodedata.normalize("NFC", str(value))]
    if isinstance(value, dt.datetime):
        return ["ts", as_utc(value).isoformat()]
    if isinstance(value, dt.date):
        return ["date", value.isoformat()]
    if isinstance(value, uuid.UUID):
        return ["uuid", str(value)]
    if isinstance(value, float):
        raise TypeError("a natural key cannot hold a float; use Decimal")
    raise TypeError(f"a natural key cannot hold a {type(value).__name__}")


def canonical_key(natural_key: tuple[object, ...]) -> bytes:
    """The bytes ``derived_id`` hashes: equal canonical keys give equal bytes.

    Numbers are normalized and rendered in plain form (``1E-7``, ``0.0000001`` and
    ``0.00000010000`` are one value, and so are ``3`` and ``Decimal("3")``; a bool keeps
    its own tag), dates and aware datetimes are ISO (datetimes in UTC), strings are NFC.
    A float or any other type is refused.
    """
    parts = [_canonical_part(part) for part in natural_key]
    return json.dumps(parts, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def derived_id(business_date: dt.date, natural_key: tuple[object, ...]) -> uuid.UUID:
    """The deterministic id of a derived row.

    The time field is ``business_date`` at 00:00 UTC. For a crypto line that is the UTC
    calendar date of ``occurred_at``, never the local date, so a time-zone setting
    change cannot re-key it. Put the row's type in the natural key when two tables
    could share one.
    """
    if isinstance(business_date, dt.datetime):
        raise TypeError("business_date is a date: pass the UTC calendar date")
    midnight = dt.datetime.combine(business_date, dt.time(), tzinfo=dt.UTC)
    digest = hashlib.sha256(canonical_key(natural_key)).digest()
    rand = int.from_bytes(digest[: _RAND_BITS // 8], "big")
    return _layout((midnight - _EPOCH) // _MS, rand)
