"""Report shape, value formats and the one output choke point (FR-35, FR-51, AC-36).

Every command, job and sink produces its text here. A report is a headline of at most
six lines, then detail. ``status`` is the one fixed list: its lines 1 to 6 are the
headline and the rest is detail, at most 15 lines in all.

``emit`` is the only way text reaches a stream, and it passes everything through
``guard`` first, so U+2014 never prints. ``emit_json`` does the same for ``--json``
output, guarding every string value and key. Sinks call ``guard`` on what they send.

This module imports neither sqlalchemy nor ``rheo_finance.storage``, so the CLI can
use it inside its import boundary.
"""

from __future__ import annotations

import datetime as dt
import json
import re
import sys
from collections.abc import Mapping, Sequence
from decimal import ROUND_HALF_EVEN, Decimal
from enum import Enum
from typing import Final, Protocol, Self
from zoneinfo import ZoneInfo

from pydantic import BaseModel

from rheo_finance.clock import Clock, render_local

HEADLINE_MAX: Final = 6
STATUS_MAX: Final = 15
HOME_CURRENCY: Final = "CAD"

_CENT: Final = Decimal("0.01")
_EM_DASH: Final = chr(0x2014)
# The characters str.splitlines() breaks on: a report line holds none of them.
LINE_BREAKS: Final = "".join(
    map(chr, (0x0A, 0x0B, 0x0C, 0x0D, 0x1C, 0x1D, 0x1E, 0x85, 0x2028, 0x2029))
)
# Horizontal whitespace: every Unicode space (tab, no-break, thin, narrow no-break
# and the rest) except a line break.
_HSPACE: Final = "[^\\S" + re.escape(LINE_BREAKS) + "]"
# A run of em dashes and the horizontal space around it becomes one plain hyphen.
_EM_DASH_RUN: Final = re.compile(
    f"{_HSPACE}*{_EM_DASH}(?:{_HSPACE}*{_EM_DASH})*{_HSPACE}*"
)
_CURRENCY: Final = re.compile(r"[A-Z0-9]{2,12}")


class Writer(Protocol):
    def write(self, text: str, /) -> object: ...


def guard(text: str) -> str:
    """``text`` with every U+2014 replaced by a plain hyphen, spaces collapsed.

    "a", an em dash, "b" becomes ``a - b`` whether or not whitespace stood around
    the dash (spaces, tabs, no-break and thin spaces alike), and a run of em dashes
    counts as one. At the start or end of a line the hyphen keeps its space only on
    the side that has text; line breaks are never touched.
    """

    def replace(match: re.Match[str]) -> str:
        start, end = match.start(), match.end()
        left = start > 0 and text[start - 1] not in LINE_BREAKS
        right = end < len(text) and text[end] not in LINE_BREAKS
        return (" " if left else "") + "-" + (" " if right else "")

    if _EM_DASH not in text:
        return text
    return _EM_DASH_RUN.sub(replace, text)


def _check_line(line: str) -> str:
    if not isinstance(line, str):
        raise TypeError(f"a report line must be str, not {type(line).__name__}")
    if any(char in LINE_BREAKS for char in line):
        raise ValueError("a report line holds no line break; pass one item per line")
    return line


class Report:
    """A headline of 1 to 6 lines, then detail lines.

    The lines are stored as tuples, so the budget checked here still holds when the
    report is emitted.
    """

    __slots__ = ("headline", "detail", "_fixed_list")

    headline: tuple[str, ...]
    detail: tuple[str, ...]
    _fixed_list: bool

    def __init__(self, headline: Sequence[str], detail: Sequence[str] = ()) -> None:
        self._init(headline, detail, fixed_list=False)

    def _init(
        self, headline: Sequence[str], detail: Sequence[str], *, fixed_list: bool
    ) -> None:
        if isinstance(headline, str) or isinstance(detail, str):
            raise TypeError("pass the headline and detail as lists of lines")
        head = tuple(_check_line(line) for line in headline)
        rest = tuple(_check_line(line) for line in detail)
        if not head:
            raise ValueError("a report starts with a headline of at least one line")
        if len(head) > HEADLINE_MAX:
            raise ValueError(
                f"a headline is at most {HEADLINE_MAX} lines; this one has {len(head)}"
            )
        object.__setattr__(self, "headline", head)
        object.__setattr__(self, "detail", rest)
        object.__setattr__(self, "_fixed_list", fixed_list)

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError("a Report is immutable")

    @classmethod
    def from_status_lines(cls, lines: Sequence[str]) -> Self:
        """The ``status`` list: lines 1 to 6 are its headline, the rest its detail."""
        if isinstance(lines, str):
            raise TypeError("pass the status lines as a list")
        if len(lines) > STATUS_MAX:
            raise ValueError(
                f"status is at most {STATUS_MAX} lines; this one has {len(lines)}"
            )
        report = cls.__new__(cls)
        report._init(lines[:HEADLINE_MAX], lines[HEADLINE_MAX:], fixed_list=True)
        return report

    @classmethod
    def from_message(cls, text: str) -> Self:
        """A message that may span lines: its first line is the headline, the rest
        detail, so a long failure still keeps the headline budget."""
        first, *rest = text.splitlines() or [""]
        return cls([first or "failed."], rest)

    def lines(self) -> list[str]:
        """Every line in print order, without the separator."""
        return [*self.headline, *self.detail]

    def text(self) -> str:
        """The report as printed: a blank line separates headline from detail,
        except in the fixed ``status`` list, which prints as one block."""
        if self.detail and not self._fixed_list:
            body = [*self.headline, "", *self.detail]
        else:
            body = self.lines()
        return "\n".join(body) + "\n"

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Report):
            return NotImplemented
        return (self.headline, self.detail, self._fixed_list) == (
            other.headline,
            other.detail,
            other._fixed_list,
        )

    def __hash__(self) -> int:
        return hash((self.headline, self.detail, self._fixed_list))

    def __repr__(self) -> str:
        return f"Report(headline={list(self.headline)!r}, detail={list(self.detail)!r})"


def _require_decimal(amount: object) -> Decimal:
    if isinstance(amount, bool) or not isinstance(amount, Decimal):
        raise TypeError(f"money takes a Decimal, not {type(amount).__name__}")
    if not amount.is_finite():
        raise ValueError("money takes a finite amount")
    return amount


def money(amount: Decimal, currency: str) -> str:
    """``1,234.56 CAD``: CAD rounded half-even to the cent, other currencies exact."""
    value = _require_decimal(amount)
    if not isinstance(currency, str) or not _CURRENCY.fullmatch(currency):
        raise ValueError("currency is an upper-case code such as CAD or BTC")
    if currency == HOME_CURRENCY:
        value = value.quantize(_CENT, rounding=ROUND_HALF_EVEN)
    if value.is_zero():
        value = abs(value)  # no "-0.00"
    return f"{value:,f} {currency}"


def iso_date(day: dt.date) -> str:
    """``YYYY-MM-DD``. An instant is refused: render it with ``as_of`` or the clock."""
    if isinstance(day, dt.datetime) or not isinstance(day, dt.date):
        raise TypeError("iso_date takes a date, not a datetime")
    return day.isoformat()


def as_of(clock: Clock, tz: ZoneInfo) -> str:
    """``as of YYYY-MM-DD HH:MM ZZZ`` at the clock's now, in ``tz``."""
    return "as of " + render_local(clock.now_utc(), tz)


def cap_rows(rows: Sequence[str], max_rows: int, more_label: str = "more") -> list[str]:
    """At most ``max_rows`` rows, then one ``+N more`` line for the rest."""
    if max_rows < 1:
        raise ValueError("max_rows is at least 1")
    if len(rows) <= max_rows:
        return list(rows)
    hidden = len(rows) - max_rows
    return [*rows[:max_rows], f"+{hidden} {more_label}"]


def emit(report_or_text: Report | str, stream: Writer | None = None) -> None:
    """Write a report or text to ``stream`` (stdout by default), guarded.

    The one choke point: every character passes through ``guard`` before it is
    written, and the output ends with a line break.
    """
    if isinstance(report_or_text, Report):
        text = report_or_text.text()
    elif isinstance(report_or_text, str):
        text = report_or_text
    else:
        kind = type(report_or_text).__name__
        raise TypeError(f"emit takes a Report or str, not {kind}")
    out = guard(text)
    if not out.endswith("\n"):
        out += "\n"
    (stream or sys.stdout).write(out)


def emit_error(report_or_text: Report | str) -> None:
    """``emit`` to stderr: the way commands report a failure."""
    emit(report_or_text, sys.stderr)


_URL_PASSWORD: Final = re.compile(
    r"(?P<head>[A-Za-z][A-Za-z0-9+.-]*://[^\s/@:]*:)[^\s@]*@"
)
_KEYWORD_PASSWORD: Final = re.compile(
    r"(?i)(?P<key>pass(?:word|wd)?|pwd)(?P<eq>\s*=\s*)(?:'[^']*'|\"[^\"]*\"|\S+)"
)
_SQL_ECHO: Final = re.compile(r"\s*\[(?:SQL|parameters|generated in)\b.*$", re.DOTALL)
_FAILURE_MAX: Final = 200


def redact(text: str) -> str:
    """``text`` with DSN and keyword passwords replaced by ``<redacted>``."""
    text = _URL_PASSWORD.sub(r"\g<head><redacted>@", text)
    return _KEYWORD_PASSWORD.sub(r"\g<key>\g<eq><redacted>", text)


def failure_line(label: str, exc: BaseException) -> str:
    """One safe line for an unexpected failure: the exception type and the first
    line of its message, with any SQL or bound parameters cut and passwords
    redacted. Nothing past the first line is shown."""
    lines = str(exc).splitlines()
    first = _SQL_ECHO.sub("", lines[0]).strip() if lines else ""
    first = redact(first)
    if len(first) > _FAILURE_MAX:
        first = first[: _FAILURE_MAX - 3] + "..."
    detail = f"{type(exc).__name__}: {first}" if first else type(exc).__name__
    return f"{label}: failed. {detail}"


def _json_safe(value: object) -> object:
    if isinstance(value, BaseModel):
        return _json_safe(value.model_dump(mode="python"))
    if isinstance(value, Enum):
        return _json_safe(value.value)
    if isinstance(value, str):
        return guard(value)
    if value is None or isinstance(value, bool | int):
        return value
    if isinstance(value, float):
        raise TypeError("JSON output takes Decimal, never float")
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError("JSON output takes a finite Decimal")
        return format(value, "f")
    if isinstance(value, dt.datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("a naive datetime has no time zone; pass an aware one")
        return value.isoformat()
    if isinstance(value, dt.date):
        return value.isoformat()
    if isinstance(value, Mapping):
        safe: dict[str, object] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError("JSON object keys are str")
            guarded = guard(key)
            if guarded in safe:
                raise ValueError(f"two JSON keys read {guarded!r} once guarded")
            safe[guarded] = _json_safe(item)
        return safe
    if isinstance(value, list | tuple):
        return [_json_safe(item) for item in value]
    raise TypeError(f"no JSON form for {type(value).__name__}")


def json_text(value: object) -> str:
    """``value`` as JSON with every string value and key guarded.

    Decimals print as plain-form strings (``0.0000001``, never a float), dates and
    aware datetimes as ISO strings.
    """
    return json.dumps(_json_safe(value), ensure_ascii=False, indent=2) + "\n"


def emit_json(value: object, stream: Writer | None = None) -> None:
    """The ``--json`` form of ``emit``."""
    emit(json_text(value), stream)
