"""``normalize_description``: NFC, casefold, whitespace collapsed (FR-4)."""

from __future__ import annotations

import pytest

from rheo_finance.text import normalize_description


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("POS PURCHASE   SAMPLE  GROCER  ", "pos purchase sample grocer"),
        ("Tab\tand\nnewline", "tab and newline"),
        ("non breaking space", "non breaking space"),
        ("STRASSE", "strasse"),
        ("Straße", "strasse"),
        # NFC: a decomposed e-acute and the composed one are one string.
        ("Café", "café"),
        ("CAFÉ", "café"),
        ("", ""),
        ("   ", ""),
        # Digits and punctuation stay: a reference number tells two lines apart.
        ("Pay #99901 / 2026-02-01", "pay #99901 / 2026-02-01"),
    ],
)
def test_the_normal_form(raw: str, expected: str) -> None:
    assert normalize_description(raw) == expected


def test_normalizing_twice_changes_nothing() -> None:
    once = normalize_description("  Straße  CAFÉ ")
    assert normalize_description(once) == once
