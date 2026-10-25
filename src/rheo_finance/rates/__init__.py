"""Stored exchange rates (FR-14), the series each line's date belongs to, and the rule
that picks a rate for a line.

``rates.valet`` fetches Bank of Canada observations into ``rate`` and records each
range a fetch covered in ``rate_coverage``; derive stage 4 reads both. Both use
``RateBook.on_or_before``, so the fetch plans exactly the lines stage 4 cannot convert.

**The series.** A currency's ``FX<CCY>CAD`` series starts on ``FX_SERIES_START``; a
date before that belongs to the legacy noon series in ``LEGACY_NOON_SERIES`` (USD only),
and a currency without one has no series there. Coverage is per series, so a line on the
last legacy days needs only legacy coverage.

**The rule.** A line converts at the last stored observation on or before its date,
only when every day from that observation to the line's date lies inside the covered
ranges of the line's series (adjacent ranges join). So:

- a weekend or holiday line inside a covered range converts at the business day before
  it: the covered days between published nothing;
- a line dated after the latest observation a fetch returned stays unconverted until a
  later fetch covers its date;
- a line in a gap between two fetched ranges stays unconverted, and the fetch plans it:
  a gap is always fetched, never bridged by an older rate.
"""

from __future__ import annotations

import bisect
import datetime as dt
from collections import defaultdict
from collections.abc import Iterable
from decimal import Decimal
from typing import Final

from sqlalchemy import select

from rheo_finance.context import UnitOfWork
from rheo_finance.storage.schema import rate, rate_coverage

QUOTE_CAD: Final = "CAD"
SOURCE_BOC: Final = "boc"
# How far before a line a fetch starts, so the reply holds the business day before a
# weekend or holiday line.
LOOKBACK_DAYS: Final = 7
# The first observation of Valet's FX<CCY>CAD series.
FX_SERIES_START: Final = dt.date(2017, 1, 3)
# The legacy noon series for dates before FX_SERIES_START, by currency. The id is the
# one the Bank's legacy noon tables used for the US dollar; it is to be confirmed at
# the first live fetch (prompt 46), and a change here changes the rates spec digest.
LEGACY_NOON_SERIES: Final[dict[str, str]] = {"USD": "IEXE0101"}
ONE_DAY: Final = dt.timedelta(days=1)


def fx_series(currency: str) -> str:
    return f"FX{currency}{QUOTE_CAD}"


def series_ids(currency: str) -> list[str]:
    legacy = LEGACY_NOON_SERIES.get(currency)
    return [fx_series(currency), *([legacy] if legacy else [])]


def series_on(currency: str, day: dt.date) -> str | None:
    """The series a ``currency`` line dated ``day`` converts with, or None."""
    if day >= FX_SERIES_START:
        return fx_series(currency)
    return LEGACY_NOON_SERIES.get(currency)


class Coverage:
    """The covered date ranges of one series, joined where they touch."""

    def __init__(self, ranges: Iterable[tuple[dt.date, dt.date]]) -> None:
        joined: list[list[dt.date]] = []
        for first, last in sorted(ranges):
            if joined and first <= joined[-1][1] + ONE_DAY:
                joined[-1][1] = max(joined[-1][1], last)
            else:
                joined.append([first, last])
        self._firsts = [first for first, _ in joined]
        self._lasts = [last for _, last in joined]

    def covers(self, first: dt.date, last: dt.date) -> bool:
        """Whether every day from ``first`` to ``last`` is covered."""
        at = bisect.bisect_right(self._firsts, first) - 1
        return at >= 0 and self._lasts[at] >= last


class RateBook:
    """One currency's stored observations in CAD from one source, and the coverage of
    each of its series."""

    def __init__(
        self,
        currency: str,
        observations: Iterable[tuple[dt.date, Decimal]],
        coverage: dict[str, Coverage],
    ) -> None:
        ordered = sorted(observations)
        self.currency = currency
        self._days = [day for day, _ in ordered]
        self._rates = [value for _, value in ordered]
        self._coverage = coverage

    def on_or_before(self, day: dt.date) -> tuple[dt.date, Decimal] | None:
        """The (date, rate) a line dated ``day`` converts at, or None."""
        series = series_on(self.currency, day)
        coverage = None if series is None else self._coverage.get(series)
        at = bisect.bisect_right(self._days, day)
        if coverage is None or at == 0:
            return None
        found = self._days[at - 1]
        if not coverage.covers(found, day):
            return None
        return found, self._rates[at - 1]


def rate_book(uow: UnitOfWork, currency: str, source: str = SOURCE_BOC) -> RateBook:
    """Every stored ``currency``/CAD observation from ``source``, and the coverage of
    the currency's series."""
    rows = uow.execute(
        select(rate.c.rate_date, rate.c.rate).where(
            rate.c.base == currency,
            rate.c.quote == QUOTE_CAD,
            rate.c.source == source,
        )
    )
    ranges: dict[str, list[tuple[dt.date, dt.date]]] = defaultdict(list)
    for each in uow.execute(
        select(
            rate_coverage.c.series,
            rate_coverage.c.covered_from,
            rate_coverage.c.covered_to,
        ).where(
            rate_coverage.c.series.in_(series_ids(currency)),
            rate_coverage.c.source == source,
        )
    ):
        ranges[each.series].append((each.covered_from, each.covered_to))
    return RateBook(
        currency,
        ((row.rate_date, row.rate) for row in rows),
        {series: Coverage(found) for series, found in ranges.items()},
    )
