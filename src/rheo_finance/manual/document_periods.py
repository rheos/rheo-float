"""Known periods of documents no parser reads, from ``document_periods.csv`` (FR-9).

``DocumentPeriodRow`` validates one row: the document's archive-relative path, the
account it belongs to (an account slug), the period it covers (end on or after start)
and an optional note. The path is relative
(no leading ``/`` or ``~``), uses ``/``, and has no empty, ``.`` or ``..`` part; it is
kept as written. ``build_document_periods`` rejects a row whose account is not in
the loaded registry, and every row of a path given twice, since a document has one
period.

Nothing is stored here. The periods are held for derivation stage 6, where an
``unparsed`` or ``needs_manual_entry`` document with a known period makes the months
it overlaps ``unparsed`` instead of leaving them silently ``missing``. The account is
entered, not derived: such a document names no account of its own, and one issuer
directory can hold several accounts.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Collection
from dataclasses import dataclass
from typing import Annotated, Final, Self

from pydantic import AfterValidator, BaseModel, ConfigDict, model_validator

from rheo_finance.manual.accounts import Slug, Text
from rheo_finance.manual.loader import LoadResult, RowError, duplicate_lines

DOCUMENT_PERIODS_FILE: Final = "document_periods.csv"


def _archive_relative(path: str) -> str:
    if path.startswith(("/", "~")):
        raise ValueError("the path is not relative to the archive")
    if "\\" in path:
        raise ValueError("the path separator is /")
    if any(part in ("", ".", "..") for part in path.split("/")):
        raise ValueError("the path has an empty, . or .. part")
    return path


ArchivePath = Annotated[Text, AfterValidator(_archive_relative)]


class DocumentPeriodRow(BaseModel):
    """One ``document_periods.csv`` row."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    archive_path: ArchivePath
    account: Slug
    period_start: dt.date
    period_end: dt.date
    note: Text | None = None

    @model_validator(mode="after")
    def _ordered(self) -> Self:
        if self.period_end < self.period_start:
            raise ValueError("period_end is before period_start")
        return self


@dataclass(frozen=True)
class DocumentPeriods:
    """The rows that passed every rule, in file order, and every error, sorted."""

    periods: tuple[DocumentPeriodRow, ...]
    errors: tuple[RowError, ...]


def build_document_periods(
    result: LoadResult[DocumentPeriodRow], account_slugs: Collection[str]
) -> DocumentPeriods:
    numbered = result.numbered()
    repeated = duplicate_lines((line, row.archive_path) for line, row in numbered)
    errors = list(result.errors)
    unknown = {line for line, row in numbered if row.account not in account_slugs}
    errors.extend(
        RowError(
            DOCUMENT_PERIODS_FILE, line, "account: no loaded account has this slug"
        )
        for line in sorted(unknown)
    )
    errors.extend(
        RowError(
            DOCUMENT_PERIODS_FILE,
            line,
            "archive_path: another row has the same document",
        )
        for line in sorted(repeated)
    )
    bad = repeated | unknown
    periods = tuple(row for line, row in numbered if line not in bad)
    return DocumentPeriods(periods, tuple(sorted(set(errors))))
