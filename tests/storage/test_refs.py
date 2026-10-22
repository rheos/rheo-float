"""``refs.parse`` and ``refs.format``: three id shapes, all else refused."""

from __future__ import annotations

import datetime as dt
import uuid

import pytest

from rheo_finance.storage import refs
from rheo_finance.storage.ids import derived_id, uuid7

FINANCE_ID = uuid7(dt.datetime(2026, 10, 22, tzinfo=dt.UTC))
UUID4 = uuid.UUID("9990a2b4-3c5d-4e6f-8a7b-9c0d1e2f3a4b")


@pytest.mark.parametrize(
    ("text", "system", "ref_type", "ref_id"),
    [
        (f"finance.decision:{FINANCE_ID}", "finance", "decision", str(FINANCE_ID)),
        (f"relationships.party:{UUID4}", "relationships", "party", str(UUID4)),
        (f"current.item:{UUID4}", "current", "item", str(UUID4)),
        ("tuttle.invoice:INV-999-1", "tuttle", "invoice", "INV-999-1"),
        ("tuttle.project:p_999/a.b", "tuttle", "project", "p_999/a.b"),
    ],
)
def test_parse_accepts_each_id_shape_and_round_trips(
    text: str, system: str, ref_type: str, ref_id: str
) -> None:
    parsed = refs.parse(text)
    assert parsed == refs.Ref(system, ref_type, ref_id)
    assert str(parsed) == text
    assert refs.format(system, ref_type, ref_id) == text


def test_a_derived_id_is_a_finance_ref() -> None:
    value = derived_id(dt.date(2026, 3, 14), ("line", 1))
    assert refs.parse(refs.format("finance", "line", value)).id == str(value)


@pytest.mark.parametrize(
    "text",
    [
        f"finance.decision:{UUID4}",  # a UUID, but not version 7
        f"finance.decision:{str(FINANCE_ID).upper()}",
        f"finance.decision:{FINANCE_ID.hex}",
        "finance.decision:not-a-uuid",
        "relationships.party:999",
        f"relationships.party:{{{UUID4}}}",
        "tuttle.invoice:",
        "tuttle.invoice:INV 999",
        "tuttle.invoice:INV\t999",
        "tuttle.invoice:INV 999",
        "tuttle.invoice:a:b",
        f"finance:{FINANCE_ID}",
        f"financedecision{FINANCE_ID}",
        f"finance.:{FINANCE_ID}",
        f".decision:{FINANCE_ID}",
        f"Finance.decision:{FINANCE_ID}",
        f"finance.a.b:{FINANCE_ID}",
        f" finance.decision:{FINANCE_ID}",
        "",
    ],
)
def test_parse_refuses_any_other_shape(text: str) -> None:
    with pytest.raises(refs.RefError):
        refs.parse(text)


def test_format_refuses_what_parse_refuses() -> None:
    with pytest.raises(refs.RefError):
        refs.format("finance", "decision", UUID4)
    with pytest.raises(refs.RefError):
        refs.format("tuttle", "invoice", "has space")
