"""The adopted-path check (SPEC Privacy and safety mechanics, "Adopted paths carry no
identifier"): whether a text may be stored or shown, given the registered identifiers.

The archive walk and the Downloads filer run it on archive-relative paths
(``documents.registry``, ``documents.filer``); the account loader runs it on each
account's slug and label (``manual.accounts``), since the report views show both. It
depends on nothing else in the package, so each of them can import it.
"""

from __future__ import annotations

import datetime as dt
import re
import unicodedata
from collections.abc import Iterable, Iterator
from typing import Final

_DIGIT_RUN = re.compile(r"\d+")
# Digit runs joined by separators, such as ``999-1234``, ``(999) 1234``,
# ``999,1234`` or ``999/1234`` (a split across a directory boundary).
_DIGIT_GROUP = re.compile(r"\d+(?:[\s._,()+/\u2013-]+\d+)+")
# An identifier that is a bare number: digits with optional spaces, dots or hyphens.
_NUMBER = re.compile(r"[\d\s.-]*\d[\d\s.-]*")
_SEPARATORS = re.compile(r"[\s._-]+")
_MASKS = re.compile(r"[*x#\u2022]")
# The years a YYYYMMDD run may name and still pass as a date. Narrower than every
# calendar date on purpose: a number that only parses as a far-off date is skipped.
_DATE_YEARS: Final = range(1900, 2101)


def _comparable(value: str) -> str:
    """``value`` NFKC-normalized (macOS file names arrive decomposed), casefolded,
    without spaces, dots, underscores or hyphens, and with every mask character
    (``*``, ``x``, ``#``, a bullet) made ``*``: so ``My_Wallet`` matches
    ``my wallet``, and ``9999-99xx-xxxx-4321`` matches ``9999 99** **** 4321``."""
    folded = unicodedata.normalize("NFKC", value).casefold()
    return _MASKS.sub("*", _SEPARATORS.sub("", folded))


def _is_date(run: str) -> bool:
    if len(run) != 8:
        return False
    try:
        day = dt.date(int(run[:4]), int(run[4:6]), int(run[6:]))
    except ValueError:
        return False
    return day.year in _DATE_YEARS


class PathCheck:
    """Whether an archive-relative path may be stored (Privacy and safety mechanics,
    "Adopted paths carry no identifier").

    A path is refused when it holds a run of 8 or more digits that is not a
    ``YYYYMMDD`` date, or contains a registered identifier. An identifier that is a bare
    number (digits with spaces, dots or hyphens) matches when its digits appear inside
    one digit run of the path, or when it equals two or more consecutive whole runs of
    one separated group (``999-1234``, ``(999) 1234`` or ``999/1234`` for
    ``9991234``: spaces, dots, underscores, hyphens, en dashes, commas, parentheses,
    plus signs and slashes separate; a run is never split, so a
    date such as ``2024-01-31`` does not hide a number from its middle). Any other
    identifier (a masked card, a file token, a
    portfolio name) matches when it appears in the path, both compared without case,
    separators or the difference between mask characters. Both are substring
    matches: they err toward skipping, and a skipped file is adopted once renamed.
    """

    def __init__(self, identifiers: Iterable[str]) -> None:
        numbers: set[str] = set()
        texts: set[str] = set()
        for identifier in identifiers:
            if _NUMBER.fullmatch(identifier):
                numbers.add("".join(ch for ch in identifier if ch.isdigit()))
            elif comparable := _comparable(identifier):
                texts.add(comparable)
        self._numbers = frozenset(numbers)
        self._texts = frozenset(texts)

    def allows(self, relative: str) -> bool:
        runs = _DIGIT_RUN.findall(relative)
        if any(len(run) >= 8 and not _is_date(run) for run in runs):
            return False
        if any(number in run for run in runs for number in self._numbers):
            return False
        if self._numbers and any(
            joined in self._numbers
            for group in _DIGIT_GROUP.findall(relative)
            for joined in _joined_runs(_DIGIT_RUN.findall(group))
        ):
            return False
        comparable = _comparable(relative)
        return not any(text in comparable for text in self._texts)


def _joined_runs(runs: list[str]) -> Iterator[str]:
    """Every concatenation of two or more consecutive runs."""
    for start in range(len(runs)):
        for end in range(start + 2, len(runs) + 1):
            yield "".join(runs[start:end])
