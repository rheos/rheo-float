"""A parsed line's hint columns, from ``reference.patterns.classify`` (FR-15).

``with_hints`` reads one line's raw description, normalized as stage 3 keys it, with
the line's account kind, its document's issuer and its sign, and fills the hint
columns. The ``document_line`` hint columns are booleans: a hint is set when
``classify`` returned one. The transfer kind and the reversal direction are not
stored (stage 8 pairs a reversal by the line's sign); ``hint_account_slug`` and
``fee_type`` are stored as returned, a fee type the layout itself names winning. A
parser never passes the owner's hint rows, so a line's parse output depends on the
repo's patterns only.

An e-Transfer line's printed name (``etransfer_name``) goes into the restricted
``counterparty`` column and nowhere else; a name the layout prints in a column of its
own, already on the line, is kept.

A parser that calls this registers with ``sets_hints=True``, so its documents are
parsed again when ``reference/patterns.json`` changes.
"""

from __future__ import annotations

import dataclasses
from decimal import Decimal

from rheo_finance.parsers.base import ParsedLine
from rheo_finance.reference.patterns import classify, etransfer_name
from rheo_finance.text import normalize_description


def _credit(amount: Decimal | None) -> bool | None:
    if amount is None or amount == 0:
        return None
    return amount > 0


def with_hints(line: ParsedLine, *, account_kind: str, issuer: str) -> ParsedLine:
    """``line`` with its hint columns (and an e-Transfer name) filled in."""
    if line.description_raw is None:
        return line
    norm = normalize_description(line.description_raw)
    hints = classify(
        norm, account_kind=account_kind, issuer=issuer, credit=_credit(line.amount)
    )
    return dataclasses.replace(
        line,
        transfer_hint=hints.transfer_hint is not None,
        reversal_hint=hints.reversal_hint is not None,
        conversion_hint=hints.conversion_hint,
        etransfer_hint=hints.etransfer_hint is not None,
        hint_account_slug=hints.hint_account_slug,
        fee_type=line.fee_type or hints.fee_type,
        counterparty=line.counterparty or etransfer_name(norm, hints),
    )
