"""Text helpers shared across stages."""

from __future__ import annotations

import re
import unicodedata
from typing import Final

_NOT_WORD: Final = re.compile(r"[\W_]+")


def normalize_description(raw: str) -> str:
    """A line's description as it is compared and keyed (FR-4).

    Unicode NFC, then ``casefold`` (so "STRASSE" and "strasse" agree), then NFC again
    (casefolding can leave a string unnormalized), then every run of whitespace, the
    non-breaking space included, collapsed to one space and the ends trimmed. Nothing
    else changes: digits, punctuation and accents stay, because a reference number in
    a description tells two same-day lines apart.
    """
    folded = unicodedata.normalize("NFC", unicodedata.normalize("NFC", raw).casefold())
    return " ".join(folded.split())


def compared(text: str) -> str:
    """``text`` folded as a party alias is (FR-57): normalized as a description is,
    then every run of punctuation, underscores and whitespace one space, ends trimmed.
    Payee patterns match in this form, and a label or slug meets its row's patterns in
    it."""
    return _NOT_WORD.sub(" ", normalize_description(text)).strip()
