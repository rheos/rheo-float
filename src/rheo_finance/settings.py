"""Typed settings: each key declared once as a ``KeySpec``, read from ``settings.toml``.

The shape follows rheoStream's ``KeySpec`` (a frozen declaration that checks its own
default), cut down to what a single-owner ledger needs: key, type, default and tier,
plus the comment ``finance init-data`` writes beside the key. There is no scope or
floor: one person owns every value.

``$FINANCE_DATA_DIR/settings.toml`` holds overrides only. A missing file means every
default. A TOML syntax error stops the command and names the line; an undeclared key or
a value of the wrong type is an error that names the key. TOML floats are read as
``Decimal`` (never ``float``), so ``5.00`` stays exact.

This module imports neither sqlalchemy nor ``rheo_finance.storage``: ``finance
init-data`` renders the settings template from it inside the CLI's import boundary.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
import tomllib
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import Final
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from rheo_finance import db
from rheo_finance.tier import Tier

SETTINGS_FILE_NAME: Final = "settings.toml"
TIMEZONE_KEY: Final = "finance.locale.timezone"
SINKS: Final[tuple[str, ...]] = (
    "mot",
    "telegram",
    "imap",
    "rates",
    "coinstats",
    "wise",
    "kraken",
    "blofin",
    "stripe",
)
WEEKDAYS: Final[tuple[str, ...]] = ("MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN")


class ValueType(StrEnum):
    STR = "str"
    INT = "int"
    BOOL = "bool"
    DECIMAL = "decimal"
    STR_LIST = "list[str]"
    DATE = "date"
    TIME = "time"
    PATH = "path"


SettingValue = str | int | bool | Decimal | dt.date | dt.time | tuple[str, ...]
"""A value as the loader returns it. A ``PATH`` key holds its text, ``~`` unexpanded,
so the effective settings hash the same on any machine; ``Settings.get_path`` expands.
"""


class SettingsError(Exception):
    """A settings file or value is wrong. The message names the line or the key."""


# finance.<section>.<key>; a sink key has one more: finance.sinks.<sink>.enabled.
_KEY_SHAPE = re.compile(r"finance(?:\.[a-z][a-z0-9_]*){2,3}")
_HH_MM = re.compile(r"([01][0-9]|2[0-3]):([0-5][0-9])")
_TOML_LINE = re.compile(r"at line (\d+)")


def _check_value(key: str, value_type: ValueType, value: object) -> SettingValue:
    """Return ``value`` as ``value_type`` or raise ``SettingsError`` naming ``key``."""
    match value_type:
        case ValueType.BOOL:
            if isinstance(value, bool):
                return value
        case ValueType.INT:
            if isinstance(value, int) and not isinstance(value, bool):
                return value
        case ValueType.DECIMAL:
            # The loader parses TOML floats as Decimal; an integer is an exact decimal.
            if isinstance(value, Decimal) and value.is_finite():
                return value
            if isinstance(value, int) and not isinstance(value, bool):
                return Decimal(value)
        case ValueType.STR | ValueType.PATH:
            if isinstance(value, str) and value.strip():
                return value
        case ValueType.STR_LIST:
            if isinstance(value, list | tuple) and all(
                isinstance(item, str) for item in value
            ):
                return tuple(value)
        case ValueType.DATE:
            # datetime is a date subclass; a timestamp is not a date setting.
            if isinstance(value, dt.date) and not isinstance(value, dt.datetime):
                return value
            if isinstance(value, str):
                try:
                    return dt.date.fromisoformat(value)
                except ValueError:
                    pass
        case ValueType.TIME:
            # Minutes are the finest grain: a time with seconds is refused, not cut.
            if (
                isinstance(value, dt.time)
                and value.tzinfo is None
                and value.second == 0
                and value.microsecond == 0
            ):
                return value
            if isinstance(value, str) and (match := _HH_MM.fullmatch(value)):
                return dt.time(int(match[1]), int(match[2]))
    raise SettingsError(
        f"setting {key}: {value!r} is not a valid {value_type.value}"
        + (' (write it as "HH:MM")' if value_type is ValueType.TIME else "")
    )


@dataclass(frozen=True, slots=True)
class KeySpec:
    """One declared settings key. ``default`` None means the key is unset by default."""

    key: str
    type: ValueType
    default: SettingValue | None
    tier: Tier
    doc: str
    choices: tuple[str, ...] | None = None

    def __post_init__(self) -> None:
        if not _KEY_SHAPE.fullmatch(self.key):
            raise ValueError(
                f"settings key {self.key!r} is not finance.<section>.<key>"
            )
        if self.default is not None:
            object.__setattr__(
                self, "default", _check_value(self.key, self.type, self.default)
            )
        if self.choices is not None and self.default not in self.choices:
            raise ValueError(f"settings key {self.key!r}: default not in its choices")

    @property
    def section(self) -> str:
        return self.key.split(".")[1]

    def coerce(self, value: object) -> SettingValue:
        checked = _check_value(self.key, self.type, value)
        if self.choices is not None and checked not in self.choices:
            raise SettingsError(
                f"setting {self.key}: {value!r} is not one of {', '.join(self.choices)}"
            )
        if self.key == TIMEZONE_KEY and isinstance(checked, str):
            zone_info(checked)
        return checked


def _spec(
    key: str,
    value_type: ValueType,
    default: SettingValue | None,
    doc: str,
    *,
    tier: Tier = Tier.PUBLIC,
    choices: tuple[str, ...] | None = None,
) -> KeySpec:
    return KeySpec(key, value_type, default, tier, doc, choices)


# Short names keep the table below to one key per line or two.
STR, INT, BOOL, DEC, LIST = (
    ValueType.STR,
    ValueType.INT,
    ValueType.BOOL,
    ValueType.DECIMAL,
    ValueType.STR_LIST,
)
DATE, TIME, PATH = ValueType.DATE, ValueType.TIME, ValueType.PATH
_INTERNAL = Tier.INTERNAL

# Grouped by section, in the order of SPEC "Files, paths and settings" > Settings.
SPECS: Final[tuple[KeySpec, ...]] = (
    # paths
    _spec("finance.paths.archive", PATH, "~/Documents/Bills",
          "The document archive, adopted in place and never modified.", tier=_INTERNAL),
    _spec("finance.paths.downloads", PATH, "~/Downloads",
          "Where downloaded statements land.", tier=_INTERNAL),
    _spec("finance.paths.backup_target", PATH, None,
          "Path on the external backup volume. Unset: the external copy is skipped"
          " and reported.", tier=_INTERNAL),
    # locale
    _spec(TIMEZONE_KEY, STR, "America/Creston",
          "Zone name (never an offset) every user-facing time renders in."),
    # sinks: all nine default false and are declared whether or not the sink exists yet.
    # The `claude -p` model step is not a sink and has no key.
    *(
        _spec(f"finance.sinks.{sink}.enabled", BOOL, False,
              f"The {sink} sink. True only takes effect with an approval record"
              " from `finance sink approve`.")
        for sink in SINKS
    ),
    # ledger
    _spec("finance.import.overlap_day_tolerance", INT, 3,
          "Days two statement periods may overlap before it is a discrepancy."),
    _spec("finance.transfers.window_days", INT, 7,
          "Days between the two legs of a transfer."),
    _spec("finance.transfers.fee_tolerance_abs", DEC, Decimal("5.00"),
          "Transfer tolerance in CAD; the larger of this and the percent applies."),
    _spec("finance.transfers.fee_tolerance_pct", DEC, Decimal("1.5"),
          "Transfer tolerance as a percent of the larger leg's CAD value."),
    _spec("finance.reversals.window_days", INT, 35,
          "Days between a line and its reversal."),
    _spec("finance.conversions.window_days", INT, 1,
          "Days between the two legs of a currency conversion."),
    _spec("finance.conversions.spread_tolerance_pct", DEC, Decimal("5.0"),
          "Conversion spread tolerance in percent."),
    # payments
    _spec("finance.payments.match_tolerance", DEC, Decimal("0.01"),
          "Amount difference that still counts as an exact payment."),
    _spec("finance.payments.evidence_window_days", INT, 5,
          "Days around a due date to look for payment evidence."),
    _spec("finance.payments.notice_hold_days", INT, 31,
          "Days a payment notice holds before it is treated as unpaid."),
    _spec("finance.payments.partial_auto_kinds", LIST,
          ("arrangement_instalment", "card_statement"),
          "Obligation kinds whose partial payments apply without a confirm."),
    _spec("finance.payments.early_days", INT, 10,
          "Days before an obligation's issue date a payment may be dated."),
    _spec("finance.receipts.match_days", INT, 3,
          "Days between a receipt and the payment line it matches."),
    _spec("finance.invoices.match_days", INT, 30,
          "Days between an invoice and the payment line it matches."),
    # classification and model
    _spec("finance.classify.auto_apply_confidence", DEC, Decimal("0.85"),
          "Proposal confidence at or above which a classification applies itself."),
    _spec("finance.classify.batch_size", INT, 10,
          "Merchants per classification model call."),
    _spec("finance.extract.model_fallback", BOOL, False,
          "Fall back to the masked model extractor when no fixed rule parses a file."),
    _spec("finance.model.classify_model", STR, None,
          "Model for classification. Unset: the CLI default."),
    _spec("finance.model.suggest_model", STR, None,
          "Model for suggestions. Unset: the CLI default."),
    _spec("finance.model.extract_model", STR, None,
          "Model for extraction. Unset: the CLI default."),
    # confirm
    _spec("finance.confirm.bulk_kinds", LIST,
          ("classification", "pairing_single", "reattach_single", "presumed_paid"),
          "Confirm kinds `finance confirm bulk` may accept in one step."),
    # reminders and notifications
    _spec("finance.reminders.mot_base_url", STR, "https://rheo.ca/mot",
          "Base URL of the ticket tracker."),
    _spec("finance.reminders.min_due_date", DATE, None,
          "Obligations due before this date get no ticket and no nudge. Set it"
          " before approving the mot sink.", tier=_INTERNAL),
    _spec("finance.reminders.wake_days_before", INT, 5,
          "Days before a due date a reminder ticket wakes."),
    _spec("finance.reminders.wake_time", TIME, dt.time(8, 0),
          "Local time a reminder ticket wakes."),
    _spec("finance.notify.nudge_days_before", INT, 2,
          "Days before a due date a nudge is sent."),
    _spec("finance.notify.alert_max_age_days", INT, 35,
          "Days after which an unsent alert is dropped."),
    _spec("finance.digest.weekday", STR, "MON", "Weekday the digest goes out.",
          choices=WEEKDAYS),
    _spec("finance.digest.time", TIME, dt.time(8, 0),
          "Local time the digest goes out."),
    _spec("finance.digest.changes_max_days", INT, 35,
          "Cap in days on \"what changed\" while no digest is sent."),
    # mail
    _spec("finance.mail.imap_host", STR, "imap.gmail.com", "IMAP host."),
    _spec("finance.mail.imap_folder", STR, "[Gmail]/All Mail", "IMAP folder to read."),
    _spec("finance.mail.credential_file", PATH, None,
          "Env file holding GMAIL_USER and GMAIL_APP_PASSWORD. Unset until you set"
          " it; mail intake over IMAP needs it.", tier=_INTERNAL),
    # reports and jobs
    _spec("finance.owed.window_days", INT, 14, "Days ahead `owed` looks."),
    _spec("finance.owed.max_rows", INT, 15, "Rows `owed` prints."),
    _spec("finance.backup.stale_days", INT, 7,
          "Days after which the last backup counts as stale."),
    _spec("finance.backup.keep_dumps", INT, 30, "Dumps kept on the external volume."),
    _spec("finance.backup.local_keep", INT, 7, "Dumps kept in the data directory."),
    _spec("finance.backup.time", TIME, dt.time(2, 0), "Local time the backup runs."),
    _spec("finance.statements.grace_days", INT, 7,
          "Days past an account's cadence before a statement counts as overdue."),
    _spec("finance.waste.price_increase_pct", DEC, Decimal("5"),
          "Price rise in percent that flags a recurring charge."),
    _spec("finance.waste.recurring_min_count", INT, 3,
          "Charges before a merchant counts as recurring."),
    _spec("finance.jobs.tick_stale_minutes", INT, 60,
          "Minutes after which the last scheduled tick counts as stale."),
    # crypto and peer money
    _spec("finance.trades.window_days", INT, 2,
          "Days between the two legs of a peer trade."),
    _spec("finance.trades.assets", LIST, ("USDT",), "Assets a peer trade can be in."),
    _spec("finance.loans.term_days", INT, 14, "Expected loan term in days."),
    _spec("finance.loans.band_low_pct", DEC, Decimal("100"),
          "Lowest repayment, as a percent of principal, that closes a loan."),
    _spec("finance.loans.band_high_pct", DEC, Decimal("110"),
          "Highest repayment, as a percent of principal, that closes a loan."),
    _spec("finance.loans.partial_floor_pct", DEC, Decimal("20"),
          "Smallest partial repayment, as a percent of principal remaining."),
    _spec("finance.loans.assets", LIST, ("BTC",), "Assets a loan can be in."),
    _spec("finance.crypto.transfer_window_hours", INT, 48,
          "Hours between the two legs of a crypto transfer."),
    _spec("finance.crypto.fee_tolerance_pct", DEC, Decimal("2.0"),
          "Crypto transfer fee tolerance in percent, when a fee is unstated."),
    _spec("finance.crypto.qty_epsilon", DEC, Decimal("1E-9"),
          "Quantity difference that counts as equal."),
    # CoinStats
    _spec("finance.coinstats.base_url", STR, "https://openapiv1.coinstats.app",
          "CoinStats API base URL."),
    _spec("finance.coinstats.max_rps", INT, 2, "CoinStats requests per second."),
    _spec("finance.coinstats.page_limit", INT, 100, "CoinStats rows per page."),
    _spec("finance.coinstats.credit_budget", INT, 10000,
          "CoinStats credits per calendar month (UTC)."),
    _spec("finance.coinstats.cost_coins", INT, 8, "Credits per coins request."),
    _spec("finance.coinstats.cost_transactions", INT, 4,
          "Credits per transactions request."),
    _spec("finance.coinstats.backoff_initial_s", INT, 60,
          "First backoff in seconds after a rate limit."),
    _spec("finance.coinstats.backoff_max_s", INT, 900, "Longest backoff in seconds."),
    _spec("finance.coinstats.sync_time", TIME, dt.time(3, 0),
          "Local time the history pull runs."),
    # holdings watch
    _spec("finance.holdings.watch_hours", INT, 6,
          "Hours between holdings snapshots."),
    _spec("finance.holdings.alert_pct", DEC, Decimal("10"),
          "Move in percent that raises a holdings alert."),
    _spec("finance.holdings.top_movers", INT, 3, "Movers a holdings alert names."),
)  # fmt: skip

SPECS_BY_KEY: Final[Mapping[str, KeySpec]] = MappingProxyType(
    {spec.key: spec for spec in SPECS}
)
if len(SPECS_BY_KEY) != len(SPECS):
    raise RuntimeError("a settings key is declared twice")


def zone_info(name: str) -> ZoneInfo:
    """The zone for an IANA zone name; an offset or unknown name is an error."""
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        raise SettingsError(
            f"setting {TIMEZONE_KEY}: {name!r} is not a time zone name"
            " (use a name such as America/Creston, not an offset)"
        ) from None


@dataclass(frozen=True)
class Settings:
    """The effective settings: every declared key, from the file or its default."""

    values: Mapping[str, SettingValue | None]
    source: Path | None = None

    def get(self, key: str) -> SettingValue | None:
        if key not in SPECS_BY_KEY:
            raise SettingsError(f"setting {key} is not declared")
        return self.values[key]

    def _typed[T](self, key: str, kind: type[T]) -> T:
        value = self.get(key)
        if not isinstance(value, kind):
            raise SettingsError(f"setting {key} is unset or not a {kind.__name__}")
        return value

    def get_str(self, key: str) -> str:
        return self._typed(key, str)

    def get_int(self, key: str) -> int:
        return self._typed(key, int)

    def get_bool(self, key: str) -> bool:
        return self._typed(key, bool)

    def get_decimal(self, key: str) -> Decimal:
        return self._typed(key, Decimal)

    def get_str_list(self, key: str) -> tuple[str, ...]:
        return self._typed(key, tuple)

    def get_date(self, key: str) -> dt.date | None:
        value = self.get(key)
        return value if isinstance(value, dt.date) else None

    def get_time(self, key: str) -> dt.time:
        return self._typed(key, dt.time)

    def get_path(self, key: str) -> Path | None:
        value = self.get(key)
        return Path(value).expanduser() if isinstance(value, str) else None

    @property
    def timezone(self) -> ZoneInfo:
        return zone_info(self.get_str(TIMEZONE_KEY))

    def sink_enabled_setting(self, sink: str) -> bool:
        """The ``enabled`` setting alone. A sink also needs its approval record."""
        return self.get_bool(f"finance.sinks.{sink}.enabled")


def settings_path(data: Path | None = None) -> Path:
    return (data or db.data_dir()) / SETTINGS_FILE_NAME


def default_settings() -> Settings:
    return Settings(MappingProxyType({spec.key: spec.default for spec in SPECS}))


def _leaves(table: Mapping[str, object], prefix: str) -> Iterator[tuple[str, object]]:
    for name, value in table.items():
        key = f"{prefix}.{name}" if prefix else name
        if isinstance(value, dict) and key not in SPECS_BY_KEY:
            yield from _leaves(value, key)
        else:
            yield key, value


def parse_settings(text: str, source: Path | None = None) -> Settings:
    """Parse settings TOML text over the defaults."""
    where = source.name if source is not None else SETTINGS_FILE_NAME
    try:
        data = tomllib.loads(text, parse_float=Decimal)
    except tomllib.TOMLDecodeError as exc:
        found = _TOML_LINE.search(str(exc))
        line = f"line {found[1]}" if found else "an unknown line"
        raise SettingsError(f"{where} has a syntax error at {line}: {exc}") from None
    values: dict[str, SettingValue | None] = {spec.key: spec.default for spec in SPECS}
    for key, raw in _leaves(data, ""):
        spec = SPECS_BY_KEY.get(key)
        if spec is None:
            raise SettingsError(f"{where}: unknown setting {key}")
        values[key] = spec.coerce(raw)
    return Settings(MappingProxyType(values), source)


def load_settings(path: Path | None = None) -> Settings:
    """Read ``path``, default ``$FINANCE_DATA_DIR/settings.toml``; missing: defaults."""
    target = path or settings_path()
    try:
        text = target.read_text(encoding="utf-8")
    except FileNotFoundError:
        return default_settings()
    return parse_settings(text, target)


def _canonical(value: SettingValue | None) -> object:
    if value is None:
        return None
    if isinstance(value, bool | int | str):
        return value
    if isinstance(value, Decimal):
        # 5, 5.0 and 5.00 are one setting; render the normalized value in plain form.
        return format(value.normalize(), "f")
    if isinstance(value, tuple):
        return list(value)
    return value.isoformat()


def settings_hash(settings: Settings) -> str:
    """A stable sha256 hex digest of the effective settings (every declared key)."""
    body = json.dumps(
        {key: _canonical(settings.values[key]) for key in sorted(SPECS_BY_KEY)},
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


# --- the settings.toml template ------------------------------------------------------


def _toml_value(spec: KeySpec, value: SettingValue) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | Decimal):
        return str(value)
    if isinstance(value, tuple):
        return "[" + ", ".join(json.dumps(item) for item in value) + "]"
    if isinstance(value, dt.time):
        return json.dumps(value.strftime("%H:%M"))
    if isinstance(value, dt.date):
        return value.isoformat()
    return json.dumps(value)


_EXAMPLES: Final[Mapping[ValueType, SettingValue]] = MappingProxyType(
    {
        ValueType.STR: "name",
        ValueType.PATH: "/path/to/it",
        ValueType.DATE: dt.date(2026, 1, 1),
    }
)


def settings_template() -> str:
    """A ``settings.toml`` with every key commented out at its default.

    Every line is a comment, so the file loads as every default. Uncommenting a key line
    gives valid TOML, because each key is written in full dotted form with no table
    headers.
    """
    lines = [
        "# Float settings. Every key below is commented out and shows its default;",
        "# a commented key takes that default. To change one, remove the leading",
        '# "# " from its line and edit the value. Times are local, "HH:MM".',
        "# An unknown key or a wrong type stops every finance command with an error.",
    ]
    section = ""
    for spec in SPECS:
        if spec.section != section:
            section = spec.section
            lines += ["", f"# Section: {section}"]
        lines.append(f"# {spec.doc}")
        if spec.default is None:
            example = _toml_value(spec, _EXAMPLES[spec.type])
            lines.append("# Unset by default. For example:")
            lines.append(f"# {spec.key} = {example}")
        else:
            lines.append(f"# {spec.key} = {_toml_value(spec, spec.default)}")
    return "\n".join(lines) + "\n"
