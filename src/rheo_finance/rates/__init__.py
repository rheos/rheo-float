"""Stored exchange rates (FR-14) and the rule that picks one for a line's date.

``rates.valet`` fetches Bank of Canada observations into the ``rate`` table under the
``rates`` sink; derive stage 4 reads them. Both use ``Observations.on_or_before``, so
the fetch plans exactly the requests that leave a line without a usable rate.

The rule: a line uses the newest stored observation on or before its date. An
observation on the date itself is always usable. An earlier one is usable only when it
is at most ``RATE_GAP_DAYS`` days older and a later observation is stored too. The
later one proves the days between were fetched and had no observation (a weekend or a
holiday), rather than not fetched yet, so a line dated after the newest fetch waits as
``unconverted`` instead of taking a rate from days before. The gap bound covers the
longest run of days the Bank publishes nothing (a weekend joined to the year-end
holidays) and keeps a hole between two fetched ranges from being bridged.
"""

from __future__ import annotations

import bisect
import datetime as dt
from collections.abc import Iterable
from decimal import Decimal
from typing import Final

from sqlalchemy import select

from rheo_finance.context import UnitOfWork
from rheo_finance.storage.schema import rate

RATE_GAP_DAYS: Final = 5
QUOTE_CAD: Final = "CAD"
SOURCE_BOC: Final = "boc"


class Observations:
    """One base currency's stored observations in CAD from one source, by date."""

    def __init__(self, observations: Iterable[tuple[dt.date, Decimal]]) -> None:
        ordered = sorted(observations)
        self._days = [day for day, _ in ordered]
        self._rates = [value for _, value in ordered]

    def __len__(self) -> int:
        return len(self._days)

    def on_or_before(self, day: dt.date) -> tuple[dt.date, Decimal] | None:
        """The usable (date, rate) for a line dated ``day``, or None."""
        at = bisect.bisect_right(self._days, day)
        if at == 0:
            return None
        found = self._days[at - 1]
        if found != day and (
            (day - found).days > RATE_GAP_DAYS or at == len(self._days)
        ):
            return None
        return found, self._rates[at - 1]


def stored(uow: UnitOfWork, base: str, source: str = SOURCE_BOC) -> Observations:
    """Every stored ``base``/CAD observation from ``source``."""
    rows = uow.execute(
        select(rate.c.rate_date, rate.c.rate).where(
            rate.c.base == base, rate.c.quote == QUOTE_CAD, rate.c.source == source
        )
    )
    return Observations((row.rate_date, row.rate) for row in rows)
