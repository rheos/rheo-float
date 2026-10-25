"""The owner's labels for crypto movements nothing pairs, from ``crypto_labels.csv``
(FR-15(b), edge cases 24 and 40; SPEC "Files, paths and settings", Derivation stage
11).

``CryptoLabelRow`` validates one row: the line it labels (a line ref,
``finance.txn:<uuid>``, as ``finance lines`` and ``finance confirm`` print it), the
label (``own_untracked``: a move to an own wallet nothing tracks; ``other``: anything
else the owner has accounted for), and an optional note. The line ref is held as the
uuid it names, which resolves through ``txn_source.line_key_id``, so a label survives
a rebuild as a decision does. ``build_crypto_labels`` rejects every row of a line ref
given twice, since one line has one label.

Stage 2 writes the rows into ``crypto_label``. Stage 9 leaves a labelled crypto line
out of every pairing, and stage 11 gives it ``transfer_state = paired`` with
``pair_kind = label``: out of every total and off the unresolved count, raising no
flagged-withdrawal event.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Annotated, Final, Literal

from pydantic import BaseModel, BeforeValidator, ConfigDict

from rheo_finance.manual.accounts import Text
from rheo_finance.manual.loader import LoadResult, RowError, duplicate_lines
from rheo_finance.storage import refs

CRYPTO_LABELS_FILE: Final = "crypto_labels.csv"
LINE_REF_TYPE: Final = "txn"

Label = Literal["own_untracked", "other"]


def _line_key(value: object) -> object:
    """A line ref cell to the uuid it names (a pydantic ``BeforeValidator``)."""
    if not isinstance(value, str):
        return value
    try:
        parsed = refs.parse(value)
    except refs.RefError:
        raise ValueError("not a line ref (finance.txn:<uuid>)") from None
    if parsed.system != refs.FINANCE_SYSTEM or parsed.type != LINE_REF_TYPE:
        raise ValueError("not a line ref (finance.txn:<uuid>)")
    return uuid.UUID(parsed.id)


class CryptoLabelRow(BaseModel):
    """One ``crypto_labels.csv`` row; ``line_ref`` is held as the line key it names."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    line_ref: Annotated[uuid.UUID, BeforeValidator(_line_key)]
    label: Label
    note: Text | None = None


@dataclass(frozen=True)
class CryptoLabels:
    """The rows that passed every rule, one per line, and every error, sorted."""

    rows: tuple[CryptoLabelRow, ...]
    errors: tuple[RowError, ...]


def build_crypto_labels(result: LoadResult[CryptoLabelRow]) -> CryptoLabels:
    numbered = result.numbered()
    repeated = duplicate_lines((line, row.line_ref) for line, row in numbered)
    errors = [
        *result.errors,
        *(
            RowError(CRYPTO_LABELS_FILE, line, "line_ref: another row labels it")
            for line in sorted(repeated)
        ),
    ]
    rows = tuple(row for line, row in numbered if line not in repeated)
    return CryptoLabels(rows, tuple(sorted(set(errors))))
