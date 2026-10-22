"""What a sink would send or request, and the clock-free digest an approval pins.

Each sink registers a ``SinkPreview`` in ``PREVIEW_BUILDERS``: its ``items`` builder
(``preview_builder(uow) -> list[PreviewItem]``) and, optionally, a note per item that
may read the clock (a reminder's wake moment) and a sample for when nothing is pending.

A ``PreviewItem`` holds only data-derived fields: what the item is, its stable key, an
amount and due date where it has them, and other data fields as text (a ticket's title
and status, a pull's series set and range start, never a range end). It has no field
for an instant, so nothing that depends on "now" can reach the digest.
``preview_digest`` is sha256 over the canonical JSON of the items, sorted by kind and
key. The rendered text (the "as of" line, relative ages, each item's note) carries the
clock and is left out of the digest, so a recomputation a minute later matches, while a
changed ticket, notice, amount or request item does not.

No builder is registered here; a sink's module registers one, and tests register a
test-only one with monkeypatch.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Final

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from rheo_finance.clock import local_today
from rheo_finance.context import FinanceContext, UnitOfWork
from rheo_finance.decision import Money
from rheo_finance.render import Report, as_of, iso_date, money
from rheo_finance.sinks.spec import SinkSpec, spec_digest

_KIND: Final = re.compile(r"[a-z][a-z_]*")
_TEXT: Final = re.compile(r"[^\x00-\x1f\x7f\x85  ]+")


def _one_line(value: str) -> str:
    if not _TEXT.fullmatch(value):
        raise ValueError("a preview value is one line of text")
    return value


class PreviewItem(BaseModel):
    """One thing a sink would send or request. Data-derived fields only."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: str
    key: str
    amount: Money | None = None
    currency: str | None = None
    due_on: dt.date | None = None
    fields: tuple[tuple[str, str], ...] = ()

    @field_validator("kind")
    @classmethod
    def _kind(cls, value: str) -> str:
        if not _KIND.fullmatch(value):
            raise ValueError("a preview kind is lower case letters and _")
        return value

    @field_validator("key")
    @classmethod
    def _key(cls, value: str) -> str:
        return _one_line(value)

    @field_validator("fields")
    @classmethod
    def _fields(cls, value: tuple[tuple[str, str], ...]) -> tuple[tuple[str, str], ...]:
        names = [name for name, _ in value]
        if len(set(names)) != len(names):
            raise ValueError("fields names a key twice")
        for name, text in value:
            if not _KIND.fullmatch(name):
                raise ValueError("a field name is lower case letters and _")
            _one_line(text)
        return tuple(sorted(value))

    @model_validator(mode="after")
    def _money(self) -> PreviewItem:
        if (self.amount is None) != (self.currency is None):
            raise ValueError("an amount comes with its currency")
        return self


def canonical_items(items: Sequence[PreviewItem]) -> str:
    """The items as canonical JSON: sorted by kind and key, sorted keys, ASCII only.

    Two items with the same kind and key are refused: the digest would hide one."""
    ordered = sorted(items, key=lambda item: (item.kind, item.key))
    pairs = [(item.kind, item.key) for item in ordered]
    if len(set(pairs)) != len(pairs):
        raise ValueError("two preview items share a kind and key")
    return json.dumps(
        [item.model_dump(mode="json") for item in ordered],
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )


def preview_digest(items: Sequence[PreviewItem]) -> str:
    """sha256 hex of the canonical items. No clock input of any kind."""
    return hashlib.sha256(canonical_items(items).encode("ascii")).hexdigest()


# The sample shown when nothing is pending: a fixed template, never ledger data.
SAMPLE_ITEMS: Final[tuple[PreviewItem, ...]] = (
    PreviewItem(
        kind="sample",
        key="sample:1",
        amount=Decimal("99.00"),
        currency="CAD",
        due_on=dt.date(2026, 1, 31),
        fields=(("title", "Sample bill due"),),
    ),
)


ItemNote = Callable[[FinanceContext, PreviewItem], str | None]


@dataclass(frozen=True)
class SinkPreview:
    """A sink's preview: the items it would send, and how to show them.

    ``note`` adds clock-dependent words to an item's line (shown, never digested).
    """

    items: Callable[[UnitOfWork], list[PreviewItem]]
    note: ItemNote | None = None
    sample: tuple[PreviewItem, ...] = field(default=SAMPLE_ITEMS)


# sink name -> its preview. Empty until a sink's module registers one.
PREVIEW_BUILDERS: dict[str, SinkPreview] = {}


@dataclass(frozen=True)
class BuiltPreview:
    items: list[PreviewItem]
    digest: str
    report: Report


def _age(due_on: dt.date, today: dt.date) -> str:
    days = (due_on - today).days
    if days == 0:
        return "due today"
    if days > 0:
        return f"due in {days} day(s)"
    return f"{-days} day(s) overdue"


def _line(
    ctx: FinanceContext, item: PreviewItem, today: dt.date, note: ItemNote | None
) -> str:
    parts: list[str] = []
    if item.amount is not None and item.currency is not None:
        parts.append(money(item.amount, item.currency))
    if item.due_on is not None:
        parts.append(f"due {iso_date(item.due_on)} ({_age(item.due_on, today)})")
    parts += [f"{name} {text}" for name, text in item.fields]
    extra = note(ctx, item) if note is not None else None
    if extra:
        parts.append(extra)
    body = "; ".join(parts)
    return f"- {item.kind} {item.key}" + (f": {body}" if body else "")


def build_preview(
    ctx: FinanceContext,
    uow: UnitOfWork,
    sink: str,
    source: SinkPreview,
    spec: SinkSpec | None,
    *,
    setting_enabled: bool,
) -> BuiltPreview:
    """Build the items, their digest and the report a person reads. Sends nothing."""
    items = list(source.items(uow))
    digest = preview_digest(items)
    tz = ctx.settings.timezone
    today = local_today(ctx.clock, tz)
    pending = f"{len(items)} item(s) pending" if items else "nothing pending"
    headline = [
        f"finance sink preview {sink}: {pending}; this preview sends nothing.",
        as_of(ctx.clock, tz),
        f"Preview digest {digest[:8]} (full {digest}).",
        f"Setting finance.sinks.{sink}.enabled is"
        f" {'true' if setting_enabled else 'false'}.",
    ]
    detail: list[str] = []
    if spec is None:
        detail.append("Spec: none declared.")
    else:
        detail.append(
            f"Spec {spec_digest(spec)[:8]}; hosts {', '.join(spec.hosts) or 'none'};"
            f" key file {spec.key_file or 'none (public)'}."
        )
        detail.append("Requests:")
        detail += [f"- {each.as_line()}" for each in spec.requests]
    if items:
        detail.append("Items:")
        ordered = sorted(items, key=lambda item: (item.kind, item.key))
        detail += [_line(ctx, item, today, source.note) for item in ordered]
    else:
        detail.append("Nothing pending. A sample item, from a fixed template:")
        detail += [_line(ctx, item, today, None) for item in source.sample]
    return BuiltPreview(items=items, digest=digest, report=Report(headline, detail))
