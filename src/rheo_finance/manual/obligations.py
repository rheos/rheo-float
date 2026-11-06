"""Manual obligations from ``obligations.csv`` (FR-10, FR-19 paths (3) and (5); SPEC
"Manual-entry files", Derivation stage 14; AC-10).

``ObligationRow`` validates one row: a payee (a payee slug), a kind from {``bill``,
``manual_debt``}, a period label, an invoice number, the amount due and minimum (CAD,
to the cent, never negative, the minimum no more than the amount due), a due date, an
optional archive-relative path of the document it reads (a ``needs_manual_entry``
photo, or the raw invoice email of a bill with no parser), and ``issued_on``, the bill
date as printed.

- A ``bill`` row needs ``issued_on`` and its due date. Its period is the due-date
  month, ``bill-YYYY-MM``: a period label left blank takes it, and one written must be
  that month (``bill-YYYY-MM``, or ``bill-YYYY-MM-<n>`` for a second bill of the month).
- A ``manual_debt`` row needs a period label, the debt's own slug; its period is
  ``debt-<slug>`` (a label written ``debt-<slug>`` is taken as is). ``issued_on`` and
  the due date may be blank: no ``issued_on`` means no lower bound on a payment's date.

``build_obligations`` then rejects a row whose payee is not a loaded payee, every row
of a (payee, kind, period) written twice, and a ``manual_debt`` whose period the
adopted-path check refuses (``path_check.PathCheck``: a run of 8 or more digits that is
not a date, or a registered identifier), since the period label is public and the
report views show it. Nothing is stored here: stage 14 reads
the rows (``ManualObligation``), each with its source reference
``manual:obligations.csv:<line_no>``.
"""

from __future__ import annotations

import datetime as dt
import re
from collections.abc import Collection
from dataclasses import dataclass
from decimal import Decimal
from pathlib import PurePosixPath
from typing import Annotated, Final, Literal, Self

from pydantic import AfterValidator, BaseModel, ConfigDict, model_validator

from rheo_finance.manual.accounts import CARRIES_IDENTIFIER, Slug, Text
from rheo_finance.manual.loader import LoadResult, RowError, duplicate_lines
from rheo_finance.path_check import PathCheck

OBLIGATIONS_FILE: Final = "obligations.csv"
SOURCE_PREFIX: Final = "manual"
DEBT_PREFIX: Final = "debt-"

ManualObligationKind = Literal["bill", "manual_debt"]

BILL_PERIOD: Final = re.compile(r"bill-(\d{4})-(\d{2})(?:-[1-9]\d*)?")
_CENTS_LIMIT: Final = Decimal(10) ** 18
SECOND_BILL_HINT: Final = (
    "; a second bill due in one month takes the label bill-YYYY-MM-2"
)


def _cents(value: Decimal) -> Decimal:
    """A CAD amount as ``numeric(20,2)`` holds it. The size comes first: ``quantize``
    raises something other than a ``ValueError`` on a huge value, which would fail the
    whole load instead of this row."""
    if value < 0:
        raise ValueError("negative")
    if value >= _CENTS_LIMIT:
        raise ValueError("too large")
    if value != value.quantize(Decimal("0.01")):
        raise ValueError("more than two decimal places")
    return value


Cents = Annotated[Decimal, AfterValidator(_cents)]


def _archive_path(value: str) -> str:
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or "\\" in value:
        raise ValueError("not a path relative to the archive")
    return str(path)


ArchivePath = Annotated[Text, AfterValidator(_archive_path)]


def bill_period(due_date: dt.date) -> str:
    """The period label of a bill due on ``due_date``: its month."""
    return f"bill-{due_date:%Y-%m}"


class ObligationRow(BaseModel):
    """One ``obligations.csv`` row."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    payee: Slug
    kind: ManualObligationKind
    period_label: Slug | None = None
    invoice_number: Text | None = None
    amount_due: Cents
    minimum: Cents | None = None
    due_date: dt.date | None = None
    archive_path: ArchivePath | None = None
    issued_on: dt.date | None = None

    @model_validator(mode="after")
    def _per_kind(self) -> Self:
        if self.minimum is not None and self.minimum > self.amount_due:
            raise ValueError("minimum: more than the amount due")
        if self.kind == "manual_debt":
            if self.period_label is None:
                raise ValueError(
                    "period_label: a manual_debt row needs the debt's slug"
                )
            return self
        if self.issued_on is None:
            raise ValueError("issued_on: a bill row needs the bill date as printed")
        if self.due_date is None:
            raise ValueError("due_date: a bill row needs its due date")
        if self.period_label is not None:
            match = BILL_PERIOD.fullmatch(self.period_label)
            if match is None or match.group(1, 2) != (
                f"{self.due_date:%Y}",
                f"{self.due_date:%m}",
            ):
                raise ValueError(
                    "period_label: a bill's period is bill-YYYY-MM of its due date"
                )
        return self

    @property
    def period(self) -> str:
        """The obligation's period label (stage 14's natural key part)."""
        if self.kind == "bill":
            assert self.due_date is not None  # the validator requires it
            return self.period_label or bill_period(self.due_date)
        assert self.period_label is not None  # the validator requires it
        if self.period_label.startswith(DEBT_PREFIX):
            return self.period_label
        return f"{DEBT_PREFIX}{self.period_label}"


def source_ref(line_no: int) -> str:
    """The source reference of the ``obligations.csv`` row on ``line_no``."""
    return f"{SOURCE_PREFIX}:{OBLIGATIONS_FILE}:{line_no}"


@dataclass(frozen=True)
class ManualObligation:
    """A loaded row and the line it is on."""

    line_no: int
    row: ObligationRow

    @property
    def source_ref(self) -> str:
        return source_ref(self.line_no)


@dataclass(frozen=True)
class ManualObligations:
    """The rows that passed every rule, in file order, and every error, sorted."""

    obligations: tuple[ManualObligation, ...]
    errors: tuple[RowError, ...]


def build_obligations(
    result: LoadResult[ObligationRow],
    payee_slugs: Collection[str],
    check: PathCheck,
) -> ManualObligations:
    errors = list(result.errors)
    rows = result.numbered()
    bad = duplicate_lines(
        [(line, (row.payee, row.kind, row.period)) for line, row in rows]
    )
    for line, row in rows:
        if line in bad:
            errors.append(
                RowError(
                    OBLIGATIONS_FILE,
                    line,
                    "period_label: another row has the same payee, kind and period"
                    + (SECOND_BILL_HINT if row.kind == "bill" else ""),
                )
            )
    for line, row in rows:
        if row.payee not in payee_slugs:
            errors.append(
                RowError(OBLIGATIONS_FILE, line, "payee: no loaded payee has this slug")
            )
            bad.add(line)
        if row.kind == "manual_debt" and not check.allows(row.period):
            errors.append(
                RowError(OBLIGATIONS_FILE, line, f"period_label: {CARRIES_IDENTIFIER}")
            )
            bad.add(line)
    kept = tuple(ManualObligation(line, row) for line, row in rows if line not in bad)
    return ManualObligations(kept, tuple(sorted(set(errors))))
