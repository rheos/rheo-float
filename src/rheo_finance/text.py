"""Text helpers shared across stages."""

from __future__ import annotations

import unicodedata


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
