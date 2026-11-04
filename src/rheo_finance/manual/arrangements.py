"""Payment arrangements from ``arrangements.csv`` (FR-10, FR-19 path (4); SPEC
"Manual-entry files", Derivation stage 14; AC-10).

``ArrangementRow`` validates one row, one instalment: the arrangement's slug, its
payee (a payee slug), ``agreed_on`` (required: it is every instalment's issue date and
the lower bound of the payments that can be its own), the instalment number (from 1),
the amount (CAD, to the cent, more than zero), the due date, and the bill periods the
arrangement replaces (``;``-separated ``bill-YYYY-MM`` labels).

``build_arrangements`` then rejects a row whose payee is not a loaded payee, a row
whose arrangement slug the adopted-path check refuses (``path_check.PathCheck``: the
slug goes into a public period label the report views show), every row of an instalment
number written twice for one arrangement, and every row of an arrangement whose rows
disagree on the payee or on ``agreed_on``. A row refused alone
(no ``agreed_on``, say) leaves the arrangement's other rows loading. Nothing is stored
here: stage 14 reads the arrangements (``Arrangement``), each instalment with its
source reference ``manual:arrangements.csv:<line_no>``.
"""

from __future__ import annotations

import datetime as dt
from collections import defaultdict
from collections.abc import Collection
from dataclasses import dataclass
from typing import Annotated, Final, Self

from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    StringConstraints,
    model_validator,
)

from rheo_finance.manual.accounts import CARRIES_IDENTIFIER, Slug
from rheo_finance.manual.loader import LoadResult, RowError, duplicate_lines, split_list
from rheo_finance.manual.obligations import BILL_PERIOD, SOURCE_PREFIX, Cents
from rheo_finance.path_check import PathCheck

ARRANGEMENTS_FILE: Final = "arrangements.csv"
INSTALMENT_PREFIX: Final = "inst-"


def _distinct(values: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(values))


# Anchored: a pydantic pattern searches, so ``xbill-2025-11`` would pass without them.
BillPeriod = Annotated[str, StringConstraints(pattern=rf"^{BILL_PERIOD.pattern}$")]
BillPeriods = Annotated[
    tuple[BillPeriod, ...], BeforeValidator(split_list), AfterValidator(_distinct)
]


class ArrangementRow(BaseModel):
    """One ``arrangements.csv`` row: one instalment."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    arrangement: Slug
    payee: Slug
    agreed_on: dt.date
    instalment: Annotated[int, Field(ge=1)]
    amount: Cents
    due_date: dt.date
    replaces_periods: BillPeriods = ()

    @model_validator(mode="after")
    def _positive(self) -> Self:
        if self.amount == 0:
            raise ValueError("amount: an instalment of zero")
        return self

    @property
    def issued_on(self) -> dt.date:
        """The instalment's issue date: the day the arrangement was agreed."""
        return self.agreed_on

    @property
    def period(self) -> str:
        """The instalment's period label, ``inst-<arrangement slug>-<n>``."""
        return f"{INSTALMENT_PREFIX}{self.arrangement}-{self.instalment}"


def source_ref(line_no: int) -> str:
    """The source reference of the ``arrangements.csv`` row on ``line_no``."""
    return f"{SOURCE_PREFIX}:{ARRANGEMENTS_FILE}:{line_no}"


@dataclass(frozen=True)
class Instalment:
    """A loaded row and the line it is on."""

    line_no: int
    row: ArrangementRow

    @property
    def source_ref(self) -> str:
        return source_ref(self.line_no)


@dataclass(frozen=True)
class Arrangement:
    """One arrangement: its loaded instalments in number order, and the bill periods
    any of them names as replaced, in first-named order."""

    slug: str
    payee: str
    agreed_on: dt.date
    instalments: tuple[Instalment, ...]
    replaces_periods: tuple[str, ...]


@dataclass(frozen=True)
class Arrangements:
    """The arrangements whose rows passed every rule, by slug, and every error,
    sorted."""

    arrangements: tuple[Arrangement, ...]
    errors: tuple[RowError, ...]


def build_arrangements(
    result: LoadResult[ArrangementRow],
    payee_slugs: Collection[str],
    check: PathCheck,
) -> Arrangements:
    errors = list(result.errors)

    def reject(line: int, reason: str) -> None:
        errors.append(RowError(ARRANGEMENTS_FILE, line, reason))

    rows = result.numbered()
    bad = duplicate_lines(
        [(line, (row.arrangement, row.instalment)) for line, row in rows]
    )
    for line in sorted(bad):
        reject(line, "instalment: another row has this arrangement and number")
    by_slug: dict[str, list[tuple[int, ArrangementRow]]] = defaultdict(list)
    for line, row in rows:
        by_slug[row.arrangement].append((line, row))
    for group in by_slug.values():
        for field_name in ("payee", "agreed_on"):
            if len({getattr(row, field_name) for _, row in group}) > 1:
                for line, _ in group:
                    reject(
                        line,
                        f"{field_name}: the rows of this arrangement disagree on it",
                    )
                    bad.add(line)
    for line, row in rows:
        if row.payee not in payee_slugs:
            reject(line, "payee: no loaded payee has this slug")
            bad.add(line)
        if not check.allows(row.arrangement):
            reject(line, f"arrangement: {CARRIES_IDENTIFIER}")
            bad.add(line)

    arrangements = []
    for slug, group in by_slug.items():
        kept = sorted(
            (Instalment(line, row) for line, row in group if line not in bad),
            key=lambda each: each.row.instalment,
        )
        if not kept:
            continue
        first = kept[0].row
        arrangements.append(
            Arrangement(
                slug=slug,
                payee=first.payee,
                agreed_on=first.agreed_on,
                instalments=tuple(kept),
                replaces_periods=_distinct(
                    tuple(p for each in kept for p in each.row.replaces_periods)
                ),
            )
        )
    return Arrangements(tuple(arrangements), tuple(sorted(set(errors))))
