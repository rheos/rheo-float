"""The settings loader: defaults, sink keys, TOML errors, unknown keys, the zone."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from pathlib import Path

import pytest

from rheo_finance.settings import (
    SINKS,
    SPECS,
    SettingsError,
    Tier,
    load_settings,
    settings_hash,
    settings_template,
)

SINK_KEYS = [f"finance.sinks.{sink}.enabled" for sink in SINKS]


def write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "settings.toml"
    path.write_text(text, encoding="utf-8")
    return path


def test_nine_sink_keys_load_false_from_an_empty_file(tmp_path: Path) -> None:
    settings = load_settings(write(tmp_path, ""))
    assert sorted(SINKS) == sorted(
        ["mot", "telegram", "imap", "rates", "coinstats", "wise", "kraken", "blofin",
         "stripe"]
    )  # fmt: skip
    for key in SINK_KEYS:
        assert settings.get(key) is False
    sink_keys = [spec.key for spec in SPECS if spec.key.startswith("finance.sinks.")]
    assert sorted(sink_keys) == sorted(SINK_KEYS)


def test_a_missing_file_means_every_default(tmp_path: Path) -> None:
    settings = load_settings(tmp_path / "settings.toml")
    assert settings.source is None
    for spec in SPECS:
        assert settings.get(spec.key) == spec.default
    assert settings.get_str("finance.locale.timezone") == "America/Creston"
    assert settings.get_date("finance.reminders.min_due_date") is None
    assert settings.get_int("finance.payments.early_days") == 10
    assert settings.get_decimal("finance.transfers.fee_tolerance_abs") == Decimal(
        "5.00"
    )
    assert settings.get_decimal("finance.transfers.fee_tolerance_pct") == Decimal("1.5")
    assert settings.get_decimal("finance.conversions.spread_tolerance_pct") == Decimal(
        "5.0"
    )
    assert settings.get_int("finance.receipts.match_days") == 3
    assert settings.get_int("finance.invoices.match_days") == 30
    assert settings.get_int("finance.holdings.watch_hours") == 6
    assert settings.get_decimal("finance.holdings.alert_pct") == Decimal("10")
    assert settings.get_int("finance.holdings.top_movers") == 3
    assert settings.get_int("finance.coinstats.credit_budget") == 10000
    assert settings.get_int("finance.jobs.tick_stale_minutes") == 60
    assert settings.get_time("finance.digest.time") == dt.time(8, 0)


def test_every_key_is_finance_section_key_and_typed() -> None:
    for spec in SPECS:
        parts = spec.key.split(".")
        assert parts[0] == "finance"
        assert len(parts) == (4 if parts[1] == "sinks" else 3), spec.key
        assert isinstance(spec.tier, Tier)
    assert not any("claude" in spec.key for spec in SPECS)


def test_the_file_overrides_defaults_and_floats_stay_decimal(tmp_path: Path) -> None:
    settings = load_settings(
        write(
            tmp_path,
            "finance.reminders.min_due_date = 2026-03-01\n"
            'finance.digest.time = "07:30"\n'
            "[finance.transfers]\n"
            "fee_tolerance_abs = 7.25\n",
        )
    )
    value = settings.get("finance.transfers.fee_tolerance_abs")
    assert isinstance(value, Decimal) and value == Decimal("7.25")
    assert settings.get_date("finance.reminders.min_due_date") == dt.date(2026, 3, 1)
    assert settings.get_time("finance.digest.time") == dt.time(7, 30)


def test_a_syntax_error_names_the_line(tmp_path: Path) -> None:
    path = write(
        tmp_path,
        "# a comment\nfinance.payments.early_days = 4\nfinance.owed.max_rows = \n",
    )
    with pytest.raises(
        SettingsError, match=r"settings\.toml has a syntax error at line 3"
    ):
        load_settings(path)


@pytest.mark.parametrize(
    ("text", "key"),
    [
        ("finance.payments.erly_days = 4\n", "finance.payments.erly_days"),
        ("[finance.sinks.nope]\nenabled = true\n", "finance.sinks.nope.enabled"),
        ("other = 1\n", "other"),
    ],
)
def test_an_unknown_key_is_an_error_naming_it(
    tmp_path: Path, text: str, key: str
) -> None:
    with pytest.raises(SettingsError, match=rf"unknown setting {key}$"):
        load_settings(write(tmp_path, text))


@pytest.mark.parametrize(
    "text",
    [
        'finance.payments.early_days = "ten"\n',
        "finance.sinks.mot.enabled = 1\n",
        "finance.payments.early_days = true\n",
        'finance.digest.weekday = "MONDAY"\n',
        'finance.backup.time = "25:00"\n',
        "finance.transfers.fee_tolerance_abs = nan\n",
    ],
)
def test_a_wrong_type_is_an_error_naming_the_key(tmp_path: Path, text: str) -> None:
    key = text.split(" = ")[0]
    with pytest.raises(SettingsError, match=rf"setting {key}: "):
        load_settings(write(tmp_path, text))


def test_the_timezone_is_a_zone_name(tmp_path: Path) -> None:
    settings = load_settings(
        write(tmp_path, 'finance.locale.timezone = "Asia/Tokyo"\n')
    )
    assert settings.timezone.key == "Asia/Tokyo"
    for bad in ("-07:00", "UTC-7", "Not/AZone"):
        with pytest.raises(SettingsError, match="not a time zone name"):
            load_settings(write(tmp_path, f'finance.locale.timezone = "{bad}"\n'))


def test_load_reads_settings_toml_from_the_data_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FINANCE_DATA_DIR", str(tmp_path))
    write(tmp_path, "finance.owed.max_rows = 9\n")
    assert load_settings().get_int("finance.owed.max_rows") == 9


def test_settings_hash_is_stable_and_follows_effective_values(tmp_path: Path) -> None:
    defaults = settings_hash(load_settings(write(tmp_path, "")))
    assert defaults == settings_hash(load_settings(tmp_path / "missing.toml"))
    # The same effective value written another way hashes the same.
    same = "finance.transfers.fee_tolerance_abs = 5\n"
    assert settings_hash(load_settings(write(tmp_path, same))) == defaults
    changed = "finance.transfers.fee_tolerance_abs = 6\n"
    assert settings_hash(load_settings(write(tmp_path, changed))) != defaults
    assert len(defaults) == 64


def test_the_template_loads_as_defaults_and_every_line_uncomments(
    tmp_path: Path,
) -> None:
    template = settings_template()
    assert chr(0x2014) not in template
    defaults = load_settings(tmp_path / "missing.toml")
    assert load_settings(write(tmp_path, template)).values == defaults.values
    uncommented = "\n".join(
        line[2:] if line.startswith("# finance.") else line
        for line in template.splitlines()
    )
    loaded = load_settings(write(tmp_path, uncommented))
    for spec in SPECS:
        assert spec.key in template
        if spec.default is not None:
            assert loaded.get(spec.key) == spec.default, spec.key
        else:
            assert loaded.get(spec.key) is not None, spec.key
