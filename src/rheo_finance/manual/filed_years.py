"""The tax years already filed, from ``filed_years.csv`` (FR-10).

``FiledYearRow`` validates one row: the year (1900 to 2100), an optional date it was
filed (after the year ended) and an optional note. ``build_filed_years`` rejects every
row of a year given twice. The default, a header-only file, is no year filed. Stage 2
stores the years in ``filed_year``; the tax packs read it for their order.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Annotated, Final, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from rheo_finance.manual.accounts import Text
from rheo_finance.manual.loader import LoadResult, RowError, duplicate_lines

FILED_YEARS_FILE: Final = "filed_years.csv"


class FiledYearRow(BaseModel):
    """One ``filed_years.csv`` row."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    year: Annotated[int, Field(ge=1900, le=2100)]
    filed_on: dt.date | None = None
    note: Text | None = None

    @model_validator(mode="after")
    def _after_year_end(self) -> Self:
        if self.filed_on is not None and self.filed_on.year <= self.year:
            raise ValueError("filed_on is before the tax year ended")
        return self


@dataclass(frozen=True)
class FiledYears:
    """The rows that passed every rule, in file order, and every error, sorted."""

    years: tuple[FiledYearRow, ...]
    errors: tuple[RowError, ...]


def build_filed_years(result: LoadResult[FiledYearRow]) -> FiledYears:
    numbered = result.numbered()
    repeated = duplicate_lines((line, row.year) for line, row in numbered)
    errors = list(result.errors)
    errors.extend(
        RowError(FILED_YEARS_FILE, line, "year: another row has the same year")
        for line in sorted(repeated)
    )
    years = tuple(row for line, row in numbered if line not in repeated)
    return FiledYears(years, tuple(sorted(set(errors))))
