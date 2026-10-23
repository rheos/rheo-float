"""The owner's transfer hint patterns, from ``transfer_hints.csv`` (FR-10, FR-15).

``TransferHintRow`` validates one row: a pattern (printed wording, normalized as a
line's ``description_norm`` is and matched as ``reference.patterns`` matches its own
wording, a whole-token run), a direction from {out, in, reversal, conversion}, and an
optional counterpart account (an account slug). ``build_transfer_hints`` rejects a row
whose counterpart is not in the loaded registry. A reversal or a conversion stays on
the line's own account, so such a row naming a counterpart is an error.

Nothing is stored: a pattern can hold a printed name, which is restricted, so the rows
live in memory only. ``reference.patterns.classify`` consults them after its built-in
patterns, and derivation stage 9 applies them to lines.
"""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass
from typing import Annotated, Final, Literal, Self

from pydantic import AfterValidator, BaseModel, ConfigDict, model_validator

from rheo_finance.manual.accounts import Slug, Text
from rheo_finance.manual.loader import LoadResult, RowError
from rheo_finance.text import normalize_description

TRANSFER_HINTS_FILE: Final = "transfer_hints.csv"

HintDirection = Literal["out", "in", "reversal", "conversion"]


def _normalized(pattern: str) -> str:
    normalized = normalize_description(pattern)
    if not normalized:
        raise ValueError("pattern is blank")
    return normalized


class TransferHintRow(BaseModel):
    """One ``transfer_hints.csv`` row; ``pattern`` is held normalized."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    pattern: Annotated[Text, AfterValidator(_normalized)]
    direction: HintDirection
    counterpart_account: Slug | None = None

    @model_validator(mode="after")
    def _counterpart_only_on_a_transfer(self) -> Self:
        if self.counterpart_account is not None and self.direction in (
            "reversal",
            "conversion",
        ):
            raise ValueError(
                "counterpart_account: a reversal or conversion row names no counterpart"
            )
        return self


@dataclass(frozen=True)
class TransferHint:
    """A loaded row and the line it is on."""

    line_no: int
    row: TransferHintRow


@dataclass(frozen=True)
class TransferHints:
    """The rows that passed every rule, in file order, and every error, sorted."""

    hints: tuple[TransferHint, ...]
    errors: tuple[RowError, ...]

    @property
    def rows(self) -> tuple[TransferHintRow, ...]:
        """The rows in file order, as ``reference.patterns.classify`` takes them."""
        return tuple(hint.row for hint in self.hints)


def build_transfer_hints(
    result: LoadResult[TransferHintRow], account_slugs: Collection[str]
) -> TransferHints:
    errors = list(result.errors)
    hints = []
    for line, row in result.numbered():
        if (
            row.counterpart_account is not None
            and row.counterpart_account not in account_slugs
        ):
            errors.append(
                RowError(
                    TRANSFER_HINTS_FILE,
                    line,
                    "counterpart_account: no loaded account has this slug",
                )
            )
            continue
        hints.append(TransferHint(line, row))
    return TransferHints(tuple(hints), tuple(sorted(set(errors))))
