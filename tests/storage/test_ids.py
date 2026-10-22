"""``derived_id`` and ``uuid7``: the UUIDv7 layout, determinism, canonical keys."""

from __future__ import annotations

import datetime as dt
import uuid
from decimal import Decimal

import pytest

from rheo_finance.storage.ids import derived_id, uuid7

DAY = dt.date(2026, 3, 14)
LOW_80 = (1 << 80) - 1


def unix_ms(instant: dt.datetime) -> int:
    epoch = dt.datetime(1970, 1, 1, tzinfo=dt.UTC)
    return (instant - epoch) // dt.timedelta(milliseconds=1)


def time_field(value: uuid.UUID) -> int:
    return value.int >> 80


def test_derived_id_is_deterministic_and_pinned() -> None:
    key = ("line", "acct-999", DAY, Decimal("-12.50"), 3)
    first = derived_id(DAY, key)
    assert derived_id(DAY, key) == first
    # Pinned: a change to the layout or the canonical form re-keys every derived row
    # and orphans every decision that points at one.
    assert str(first) == "019ce9a4-8800-7060-b61e-f314d685a29a"


def test_the_time_field_is_the_business_date_at_midnight_utc() -> None:
    value = derived_id(DAY, ("line", 1))
    assert time_field(value) == unix_ms(dt.datetime(2026, 3, 14, tzinfo=dt.UTC))
    assert value.version == 7
    assert value.variant == uuid.RFC_4122

    next_day = derived_id(dt.date(2026, 3, 15), ("line", 1))
    assert time_field(next_day) - time_field(value) == 86_400_000
    assert next_day.int & LOW_80 == value.int & LOW_80


@pytest.mark.parametrize(
    "forms",
    [
        ("1E-7", "0.0000001", "0.00000010000"),
        ("0E-10", "0", "-0", "0.000"),
        ("1E+2", "100", "100.00"),
        ("-2.5", "-2.50", "-25E-1"),
    ],
)
def test_one_decimal_value_in_any_form_gives_one_id(forms: tuple[str, ...]) -> None:
    ids = {derived_id(DAY, ("qty", Decimal(form))) for form in forms}
    assert len(ids) == 1


def test_different_decimals_give_different_ids() -> None:
    small_a, small_b = Decimal("0.0000001"), Decimal("0.0000002")
    assert derived_id(DAY, (small_a,)) != derived_id(DAY, (small_b,))
    # 31 significant digits: past the default 28-digit context, which would round both
    # quantities to one value and merge two lines into one id.
    long_a = Decimal("99912345678.12345678901234567890")
    long_b = Decimal("99912345678.12345678901234567891")
    assert derived_id(DAY, (long_a,)) != derived_id(DAY, (long_b,))


def test_an_int_and_a_decimal_of_equal_value_give_one_id() -> None:
    assert derived_id(DAY, (3,)) == derived_id(DAY, (Decimal("3"),))
    assert derived_id(DAY, (Decimal("3.00"),)) == derived_id(DAY, (3,))
    assert derived_id(DAY, (300,)) == derived_id(DAY, (Decimal("3E+2"),))
    assert derived_id(DAY, (Decimal("-0"),)) == derived_id(DAY, (0,))
    assert derived_id(DAY, (3,)) != derived_id(DAY, (4,))
    assert derived_id(DAY, (3,)) != derived_id(DAY, (Decimal("3.1"),))


def test_a_bool_keeps_its_own_tag() -> None:
    assert derived_id(DAY, (True,)) != derived_id(DAY, (1,))
    assert derived_id(DAY, (True,)) != derived_id(DAY, (Decimal("1"),))
    assert derived_id(DAY, (False,)) != derived_id(DAY, (0,))
    assert derived_id(DAY, (True,)) != derived_id(DAY, (False,))


def test_strings_are_nfc_and_parts_keep_their_type_and_boundaries() -> None:
    assert derived_id(DAY, ("Café",)) == derived_id(DAY, ("Café",))
    assert derived_id(DAY, ("ab",)) != derived_id(DAY, ("a", "b"))
    assert derived_id(DAY, ("1",)) != derived_id(DAY, (Decimal("1"),))
    assert derived_id(DAY, (DAY,)) == derived_id(DAY, (dt.date(2026, 3, 14),))
    noon_utc = dt.datetime(2026, 3, 14, 12, tzinfo=dt.UTC)
    same_instant = noon_utc.astimezone(dt.timezone(dt.timedelta(hours=-7)))
    assert derived_id(DAY, (noon_utc,)) == derived_id(DAY, (same_instant,))


def test_derived_id_refuses_what_it_cannot_canonicalize() -> None:
    with pytest.raises(TypeError, match="float"):
        derived_id(DAY, (1.5,))
    with pytest.raises(ValueError, match="naive"):
        derived_id(DAY, (dt.datetime(2026, 3, 14, 12),))
    with pytest.raises(ValueError, match="NaN"):
        derived_id(DAY, (Decimal("NaN"),))
    with pytest.raises(TypeError, match="business_date"):
        derived_id(dt.datetime(2026, 3, 14, tzinfo=dt.UTC), ("line",))
    with pytest.raises(ValueError, match="1970"):
        derived_id(dt.date(1969, 12, 31), ("line",))


def test_uuid7_is_timed_at_now_and_random_below() -> None:
    now = dt.datetime(2026, 10, 22, 6, 30, 1, 250_000, tzinfo=dt.UTC)
    first, second = uuid7(now), uuid7(now)
    assert first != second
    for value in (first, second):
        assert value.version == 7
        assert value.variant == uuid.RFC_4122
        assert time_field(value) == unix_ms(now)
    with pytest.raises(ValueError, match="naive"):
        uuid7(dt.datetime(2026, 10, 22))
    assert uuid7().version == 7
