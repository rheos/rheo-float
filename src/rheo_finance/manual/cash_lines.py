"""Cash and off-record lines from ``transactions.csv`` (FR-10, FR-13).

``CashLineRow`` validates one row: a date, a signed amount (a ``Decimal``, never zero),
a currency, a description, an optional note, and the account (an account slug), which
defaults to the built-in "Paid outside recorded accounts". ``build_cash_lines`` then
rejects a row whose account is not in the loaded registry.

Nothing is stored here. The lines are held for derivation stage 3, which turns each
into a txn with origin ``manual``; ``CashLine.source_ref`` is its source reference,
``manual:transactions.csv:<line_no>``, so no manual line exists without one (FR-13).
The reference follows the row's line, so moving a row to another line re-keys it.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Collection
from dataclasses import dataclass
from decimal import Decimal
from typing import Annotated, Final, Self

from pydantic import AfterValidator, BaseModel, ConfigDict, model_validator

from rheo_finance.manual.accounts import INTERNAL_ACCOUNT, Currency, Slug, Text
from rheo_finance.manual.loader import LoadResult, RowError

TRANSACTIONS_FILE: Final = "transactions.csv"
SOURCE_PREFIX: Final = "manual"


def source_ref(line_no: int) -> str:
    """The source reference of the ``transactions.csv`` row on ``line_no``."""
    return f"{SOURCE_PREFIX}:{TRANSACTIONS_FILE}:{line_no}"


class CashLineRow(BaseModel):
    """One ``transactions.csv`` row. ``amount`` is signed: negative is money out."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    account: Slug = INTERNAL_ACCOUNT.slug
    date: dt.date
    amount: Decimal
    currency: Annotated[Currency, AfterValidator(str.upper)]
    description: Text
    note: Text | None = None

    @model_validator(mode="after")
    def _nonzero(self) -> Self:
        if self.amount == 0:
            raise ValueError("amount is zero")
        return self


@dataclass(frozen=True)
class CashLine:
    """A loaded row and the line it is on."""

    line_no: int
    row: CashLineRow

    @property
    def source_ref(self) -> str:
        return source_ref(self.line_no)


@dataclass(frozen=True)
class CashLines:
    """The lines that passed every rule, in file order, and every error, sorted."""

    lines: tuple[CashLine, ...]
    errors: tuple[RowError, ...]


def build_cash_lines(
    result: LoadResult[CashLineRow], account_slugs: Collection[str]
) -> CashLines:
    errors = list(result.errors)
    lines = []
    for line, row in result.numbered():
        if row.account not in account_slugs:
            errors.append(
                RowError(
                    TRANSACTIONS_FILE, line, "account: no loaded account has this slug"
                )
            )
            continue
        lines.append(CashLine(line, row))
    return CashLines(tuple(lines), tuple(sorted(set(errors))))
