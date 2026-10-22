"""Decisions: the owner's answers, kept as append-only history (FR-18, Key decision 19).

``finance.decision.record`` writes one decision, its parts and legs
(``decision_item``) and a snapshot of every line it names (``decision_target``).
``finance.decision.reverse`` records a ``reverse`` decision naming one decision or a
whole bulk run; nothing is ever updated or deleted, so totals re-derive from the
reversal. ``effective`` is the helper every later stage uses: the newest unreversed
decision of a kind for a target.

``DECISION_KIND_COLUMNS`` says which columns each kind may and must use. Migration 0002
holds a frozen copy as one CHECK per kind; a prompt that widens a kind changes both, and
the decision tests fail while they disagree.
"""

from __future__ import annotations

import datetime as dt
import re
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, field_validator
from sqlalchemy import ColumnElement, Table, and_, exists, func, insert, or_, select

from rheo_finance.context import FinanceContext, UnitOfWork
from rheo_finance.contracts import Role, SafetyClass, Tiered
from rheo_finance.operations import OperationError, OperationSpec, register
from rheo_finance.storage import refs
from rheo_finance.storage.ids import uuid7
from rheo_finance.storage.schema import decision, decision_item, decision_target
from rheo_finance.tier import Tier


class DecisionKind(StrEnum):
    CATEGORY = "category"
    PAIR = "pair"
    UNPAIR = "unpair"
    SPLIT = "split"
    PROPOSAL_DECLINE = "proposal_decline"
    INCLUDE = "include"
    PAYMENT_APPLY = "payment_apply"
    PAYMENT_UNAPPLY = "payment_unapply"
    OBLIGATION_DISPUTE = "obligation_dispute"
    PRESUMED_PAID = "presumed_paid"
    INVOICE_PAID = "invoice_paid"
    INVOICE_UNPAID = "invoice_unpaid"
    FINDING_DISMISS = "finding_dismiss"
    SENDER_APPROVE = "sender_approve"
    SINK_APPROVE = "sink_approve"
    SINK_DISABLE = "sink_disable"
    REATTACH_DECLINE = "reattach_decline"
    REVERSE = "reverse"


class ItemRole(StrEnum):
    PART = "part"
    LEG = "leg"


# The columns a kind's CHECK governs; id, kind, note and decided_at are every kind's.
MATRIX_COLUMNS: tuple[str, ...] = (
    "target_ref", "category", "tag", "business_unit", "business_pct",
    "counterpart_ref", "obligation_ref", "party_ref", "account_ref", "effective_on",
    "sink", "spec_digest", "preview_digest", "amount", "about_decision_id", "bulk_id",
    "reverses_id", "reverses_bulk_id",
)  # fmt: skip


@dataclass(frozen=True)
class KindColumns:
    """The columns one kind uses. Every matrix column outside ``allowed`` is null."""

    required: frozenset[str] = frozenset()
    optional: frozenset[str] = frozenset()
    exactly_one: tuple[frozenset[str], ...] = ()
    at_least_one: tuple[frozenset[str], ...] = ()

    @property
    def allowed(self) -> frozenset[str]:
        groups = frozenset().union(*self.exactly_one, *self.at_least_one)
        return self.required | self.optional | groups


def _kc(
    required: Iterable[str] = (),
    optional: Iterable[str] = (),
    exactly_one: Iterable[Iterable[str]] = (),
    at_least_one: Iterable[Iterable[str]] = (),
) -> KindColumns:
    return KindColumns(
        frozenset(required),
        frozenset(optional),
        tuple(frozenset(group) for group in exactly_one),
        tuple(frozenset(group) for group in at_least_one),
    )


K = DecisionKind
REATTACH = "about_decision_id"
BULK = "bulk_id"
DECISION_KIND_COLUMNS: Mapping[DecisionKind, KindColumns] = {
    # A line-keyed category names target_ref; a merchant-keyed one leaves it null and
    # its merchant key lives on its decision_target row.
    K.CATEGORY: _kc(
        optional=("target_ref", "category", "tag", "business_unit", "business_pct",
                  REATTACH, BULK),
        at_least_one=(("category", "tag"),),
    ),
    K.PAIR: _kc(("target_ref",), ("counterpart_ref", "party_ref", REATTACH, BULK)),
    K.UNPAIR: _kc(("target_ref",), ("counterpart_ref", REATTACH, BULK)),
    K.SPLIT: _kc(("target_ref",), (REATTACH, BULK)),
    K.PROPOSAL_DECLINE: _kc(("target_ref",), (BULK,)),
    K.INCLUDE: _kc(("target_ref",), (REATTACH, BULK)),
    K.PAYMENT_APPLY: _kc(("target_ref", "obligation_ref"), ("amount", REATTACH, BULK)),
    K.PAYMENT_UNAPPLY: _kc(("target_ref",), ("obligation_ref", REATTACH, BULK)),
    K.OBLIGATION_DISPUTE: _kc(("obligation_ref",), (REATTACH, BULK)),
    K.PRESUMED_PAID: _kc(("obligation_ref",), (REATTACH, BULK)),
    K.INVOICE_PAID: _kc(("target_ref", "account_ref", "effective_on"), (BULK,)),
    K.INVOICE_UNPAID: _kc(("target_ref",), ("effective_on", BULK)),
    K.FINDING_DISMISS: _kc(("target_ref",), ("amount", BULK)),
    K.SENDER_APPROVE: _kc(("target_ref",), (BULK,)),
    K.SINK_APPROVE: _kc(("sink", "spec_digest", "preview_digest")),
    K.SINK_DISABLE: _kc(("sink",)),
    K.REATTACH_DECLINE: _kc((REATTACH,), ("target_ref", BULK)),
    K.REVERSE: _kc(exactly_one=(("reverses_id", "reverses_bulk_id"),)),
}  # fmt: skip

# The column ``effective`` matches a target against, per kind. ``reverse`` has none.
_OTHER_TARGETS: Mapping[DecisionKind, str] = {
    K.OBLIGATION_DISPUTE: "obligation_ref",
    K.PRESUMED_PAID: "obligation_ref",
    K.SINK_APPROVE: "sink",
    K.SINK_DISABLE: "sink",
    K.REATTACH_DECLINE: REATTACH,
}
TARGET_COLUMN: Mapping[DecisionKind, str] = {
    kind: _OTHER_TARGETS.get(kind, "target_ref")
    for kind in DecisionKind
    if kind is not K.REVERSE
}

# Kinds whose named lines get a decision_target snapshot and can be orphaned (stage 17).
LINE_KEYED_KINDS = frozenset(
    {K.CATEGORY, K.PAIR, K.UNPAIR, K.SPLIT, K.INCLUDE, K.PAYMENT_APPLY,
     K.PAYMENT_UNAPPLY}
)  # fmt: skip

# Kinds ``finance.decision.record`` refuses, with the path that records them. An
# approval reachable from record would let a job or a non-terminal path approve a sink.
RECORDED_ELSEWHERE: Mapping[DecisionKind, str] = {
    K.SINK_APPROVE: "finance sink approve",
    K.REVERSE: "finance.decision.reverse",
}


class DecisionRefused(OperationError):
    """A decision the rules refuse. Messages name kinds and fields, never values."""


# --- the operation models -----------------------------------------------------------


def _no_float(value: object) -> object:
    if isinstance(value, float):
        raise ValueError("an amount is a Decimal or a string, never a float")
    return value


Money = Annotated[Decimal, BeforeValidator(_no_float)]
Words = Annotated[str, Field(min_length=1)]
COVERS_REF = re.compile(r"rent:[0-9]{4}-(0[1-9]|1[0-2])|bill:\S+")


def _check_ref(value: str | None) -> str | None:
    if value is not None:
        refs.parse(value)
    return value


class PartIn(BaseModel):
    """One part of a ``split``: a label from FR-58's set and its amount."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    label: Words
    amount: Money
    covers_ref: str | None = None

    @field_validator("covers_ref")
    @classmethod
    def _covers(cls, value: str | None) -> str | None:
        if value is not None and not COVERS_REF.fullmatch(value):
            raise ValueError("covers_ref is rent:YYYY-MM or bill:<key>")
        return value


class LegIn(BaseModel):
    """One leg of a ``pair``: a line beyond the target and the counterpart."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    target_ref: str
    amount: Money | None = None

    _ref = field_validator("target_ref")(_check_ref)


class RecordIn(BaseModel):
    """A decision's kind, the kind's fields and the lines it names.

    A merchant-keyed ``category`` gives ``merchant_key`` and ``newest_line_ref`` (the
    merchant's newest line, whose snapshot is stored) instead of ``target_ref``.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: DecisionKind
    target_ref: str | None = None
    merchant_key: Words | None = None
    newest_line_ref: str | None = None
    category: Words | None = None
    tag: Words | None = None
    business_unit: Words | None = None
    business_pct: Annotated[Money, Field(ge=0, le=100)] | None = None
    counterpart_ref: str | None = None
    obligation_ref: str | None = None
    party_ref: str | None = None
    account_ref: str | None = None
    effective_on: dt.date | None = None
    sink: Words | None = None
    spec_digest: Words | None = None
    preview_digest: Words | None = None
    amount: Money | None = None
    about_decision_id: uuid.UUID | None = None
    bulk_id: uuid.UUID | None = None
    note: Words | None = None
    parts: tuple[PartIn, ...] = ()
    legs: tuple[LegIn, ...] = ()

    _refs = field_validator(
        "target_ref",
        "newest_line_ref",
        "counterpart_ref",
        "obligation_ref",
        "party_ref",
        "account_ref",
    )(_check_ref)


class RecordOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    decision_id: Annotated[uuid.UUID, Tiered(Tier.PUBLIC)]
    items: Annotated[int, Tiered(Tier.PUBLIC)]
    targets: Annotated[int, Tiered(Tier.PUBLIC)]


class ReverseIn(BaseModel):
    """Exactly one of ``decision_id`` and ``bulk_id``."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    decision_id: uuid.UUID | None = None
    bulk_id: uuid.UUID | None = None
    note: Words | None = None


class ReverseOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    decision_id: Annotated[uuid.UUID, Tiered(Tier.PUBLIC)]
    # How many decisions this reversal un-applied (one, or a bulk's still-applied ones).
    reversed: Annotated[int, Tiered(Tier.PUBLIC)]


class Decision(BaseModel):
    """One stored decision row."""

    model_config = ConfigDict(frozen=True)

    id: uuid.UUID
    kind: DecisionKind
    target_ref: str | None
    category: str | None
    tag: str | None
    business_unit: str | None
    business_pct: Decimal | None
    counterpart_ref: str | None
    obligation_ref: str | None
    party_ref: str | None
    account_ref: str | None
    effective_on: dt.date | None
    sink: str | None
    spec_digest: str | None
    preview_digest: str | None
    amount: Decimal | None
    about_decision_id: uuid.UUID | None
    bulk_id: uuid.UUID | None
    reverses_id: uuid.UUID | None
    reverses_bulk_id: uuid.UUID | None
    note: str | None
    decided_at: dt.datetime


# --- reading: the newest unreversed decision ----------------------------------------


@dataclass(frozen=True, slots=True)
class MerchantKey:
    """A merchant key as an ``effective`` target, so it never reads as a line ref."""

    key: str


def _reversed(row: Table) -> ColumnElement[bool]:
    """True when a ``reverse`` names ``row`` or the bulk run that recorded it."""
    rev = decision.alias("rev")
    return exists().where(
        rev.c.kind == K.REVERSE.value,
        or_(
            rev.c.reverses_id == row.c.id,
            and_(row.c.bulk_id.is_not(None), rev.c.reverses_bulk_id == row.c.bulk_id),
        ),
    )


def _bulk_reversed(uow: UnitOfWork, bulk_id: uuid.UUID) -> bool:
    """Whether a ``reverse`` names bulk run ``bulk_id``."""
    return bool(
        uow.scalar(
            select(func.count()).where(
                decision.c.kind == K.REVERSE.value,
                decision.c.reverses_bulk_id == bulk_id,
            )
        )
    )


def effective(
    uow: UnitOfWork,
    kind: DecisionKind | Iterable[DecisionKind],
    target: str | uuid.UUID | MerchantKey,
) -> Decision | None:
    """The newest unreversed decision of ``kind`` (or of any of several kinds that
    share a target column) for ``target``, or None.

    ``target`` is matched against the kind's ``TARGET_COLUMN``: a line or other ref,
    an obligation ref, a sink name, or for ``reattach_decline`` the orphan's id. A
    ``MerchantKey`` finds merchant-keyed ``category`` decisions. Newest is by
    ``decided_at``, then id.
    """
    kinds = (kind,) if isinstance(kind, DecisionKind) else tuple(kind)
    if not kinds:
        raise ValueError("effective needs at least one kind")
    columns = set()
    for each in kinds:
        column = TARGET_COLUMN.get(each)
        if column is None:
            raise ValueError(f"{each.value} decisions have no target to look up")
        columns.add(column)
    if len(columns) != 1:
        raise ValueError("effective takes kinds that share one target column")
    row = decision
    match: ColumnElement[bool]
    if isinstance(target, MerchantKey):
        if set(kinds) != {K.CATEGORY}:
            raise ValueError("a merchant key is the target of category decisions only")
        match = and_(
            row.c.target_ref.is_(None),
            exists().where(
                decision_target.c.decision_id == row.c.id,
                decision_target.c.merchant_key == target.key,
            ),
        )
    else:
        match = row.c[columns.pop()] == target
    statement = (
        select(row)
        .where(row.c.kind.in_([each.value for each in kinds]), match, ~_reversed(row))
        .order_by(row.c.decided_at.desc(), row.c.id.desc())
        .limit(1)
    )
    found = uow.execute(statement).mappings().first()
    return None if found is None else Decision.model_validate(dict(found))


# --- finance.decision.record ----------------------------------------------------------


def _check_columns(data: RecordIn) -> None:
    kind = data.kind
    rules = DECISION_KIND_COLUMNS[kind]
    present = {
        column for column in MATRIX_COLUMNS if getattr(data, column, None) is not None
    }
    missing = sorted(rules.required - present)
    if missing:
        raise DecisionRefused(f"{kind.value}: needs {', '.join(missing)}")
    unused = sorted(present - rules.allowed)
    if unused:
        raise DecisionRefused(f"{kind.value}: does not use {', '.join(unused)}")
    for group in rules.exactly_one:
        if len(group & present) != 1:
            raise DecisionRefused(
                f"{kind.value}: needs exactly one of {', '.join(sorted(group))}"
            )
    for group in rules.at_least_one:
        if not group & present:
            raise DecisionRefused(
                f"{kind.value}: needs at least one of {', '.join(sorted(group))}"
            )

    merchant = data.merchant_key is not None or data.newest_line_ref is not None
    if kind is K.CATEGORY:
        if (data.target_ref is None) == (data.merchant_key is None):
            raise DecisionRefused(
                "category: names exactly one of target_ref and merchant_key"
            )
        if (data.merchant_key is None) != (data.newest_line_ref is None):
            raise DecisionRefused(
                "category: a merchant_key comes with the merchant's newest_line_ref"
            )
    elif merchant:
        raise DecisionRefused(
            f"{kind.value}: does not use merchant_key or newest_line_ref"
        )
    if data.parts and kind is not K.SPLIT:
        raise DecisionRefused(f"{kind.value}: does not take parts")
    if kind is K.SPLIT and not data.parts:
        raise DecisionRefused("split: needs at least one part")
    if data.legs and kind is not K.PAIR:
        raise DecisionRefused(f"{kind.value}: does not take legs")


def _check_about(uow: UnitOfWork, data: RecordIn) -> None:
    """The orphan a re-attachment or a decline answers exists; a re-attachment is a
    decision of the orphan's own kind."""
    if data.about_decision_id is None:
        return
    about_kind = uow.scalar(
        select(decision.c.kind).where(decision.c.id == data.about_decision_id)
    )
    if about_kind is None:
        raise DecisionRefused(f"{data.kind.value}: about_decision_id names no decision")
    if data.kind is K.REATTACH_DECLINE:
        if DecisionKind(about_kind) not in LINE_KEYED_KINDS | {
            K.OBLIGATION_DISPUTE,
            K.PRESUMED_PAID,
        }:
            raise DecisionRefused(
                "reattach_decline: about_decision_id names a decision that cannot"
                " be orphaned"
            )
    elif about_kind != data.kind.value:
        raise DecisionRefused(
            f"{data.kind.value}: a re-attachment has the orphan's own kind"
        )


def _named_lines(data: RecordIn) -> list[tuple[str, str]]:
    """``(field, ref)`` for every line the decision names, first mention only."""
    if data.kind not in LINE_KEYED_KINDS:
        return []
    named: list[tuple[str, str | None]] = [
        ("target_ref", data.target_ref),
        ("newest_line_ref", data.newest_line_ref),
        ("counterpart_ref", data.counterpart_ref),
    ]
    named += [(f"legs.{index}", leg.target_ref) for index, leg in enumerate(data.legs)]
    seen: set[str] = set()
    lines: list[tuple[str, str]] = []
    for field, ref in named:
        if ref is not None and ref not in seen:
            seen.add(ref)
            lines.append((field, ref))
    return lines


def record(ctx: FinanceContext, uow: UnitOfWork, data: RecordIn) -> RecordOut:
    elsewhere = RECORDED_ELSEWHERE.get(data.kind)
    if elsewhere is not None:
        raise DecisionRefused(f"{data.kind.value}: recorded only by {elsewhere}")
    _check_columns(data)
    _check_about(uow, data)
    if data.bulk_id is not None and _bulk_reversed(uow, data.bulk_id):
        # It would be un-applied from the moment it was recorded.
        raise DecisionRefused(f"{data.kind.value}: that bulk run is already reversed")

    snapshots = []
    for field, ref in _named_lines(data):
        snapshot = ctx.snapshot_provider(uow, ref)
        if snapshot is None:
            raise DecisionRefused(
                f"{data.kind.value}: {field} resolves to no current line"
            )
        snapshots.append((ref, snapshot))

    now = ctx.clock.now_utc()
    decision_id = uuid7(now)
    values: dict[str, object] = {
        column: getattr(data, column, None) for column in MATRIX_COLUMNS
    }
    values.update(id=decision_id, kind=data.kind.value, note=data.note, decided_at=now)
    uow.execute(insert(decision).values(**values))

    items: list[dict[str, object]] = [
        {
            "decision_id": decision_id,
            "role": ItemRole.PART.value,
            "target_ref": None,
            "label": part.label,
            "amount": part.amount,
            "covers_ref": part.covers_ref,
        }
        for part in data.parts
    ]
    items += [
        {
            "decision_id": decision_id,
            "role": ItemRole.LEG.value,
            "target_ref": leg.target_ref,
            "label": None,
            "amount": leg.amount,
            "covers_ref": None,
        }
        for leg in data.legs
    ]
    for seq, item in enumerate(items, start=1):
        item["seq"] = seq
    if items:
        uow.execute(insert(decision_item), items)

    targets = [
        {
            "decision_id": decision_id,
            "seq": seq,
            "target_ref": ref,
            "account_slug": snapshot.account_slug,
            "amount": snapshot.amount,
            "currency": snapshot.currency,
            "posting_date": snapshot.posting_date,
            "merchant_key": data.merchant_key,
        }
        for seq, (ref, snapshot) in enumerate(snapshots, start=1)
    ]
    if targets:
        uow.execute(insert(decision_target), targets)
    return RecordOut(decision_id=decision_id, items=len(items), targets=len(targets))


# --- finance.decision.reverse ---------------------------------------------------------


def reverse(ctx: FinanceContext, uow: UnitOfWork, data: ReverseIn) -> ReverseOut:
    if (data.decision_id is None) == (data.bulk_id is None):
        raise DecisionRefused("reverse: names exactly one of decision_id and bulk_id")
    if data.decision_id is not None:
        found = (
            uow.execute(
                select(decision.c.kind, _reversed(decision).label("reversed")).where(
                    decision.c.id == data.decision_id
                )
            )
            .mappings()
            .first()
        )
        if found is None:
            raise DecisionRefused("reverse: no decision has that id")
        if found["kind"] == K.REVERSE.value:
            raise DecisionRefused(
                "reverse: a reversal is not reversed; record the decision again"
            )
        if found["reversed"]:
            raise DecisionRefused("reverse: that decision is already reversed")
        undone = 1
    else:
        assert data.bulk_id is not None  # checked above: exactly one is set
        in_bulk = decision.c.bulk_id == data.bulk_id
        total = uow.scalar(select(func.count()).where(in_bulk))
        if not total:
            raise DecisionRefused("reverse: no decision has that bulk id")
        if _bulk_reversed(uow, data.bulk_id):
            raise DecisionRefused("reverse: that bulk run is already reversed")
        undone = int(
            uow.scalar(select(func.count()).where(in_bulk, ~_reversed(decision))) or 0
        )

    now = ctx.clock.now_utc()
    reversal_id = uuid7(now)
    uow.execute(
        insert(decision).values(
            id=reversal_id,
            kind=K.REVERSE.value,
            reverses_id=data.decision_id,
            reverses_bulk_id=data.bulk_id,
            note=data.note,
            decided_at=now,
        )
    )
    return ReverseOut(decision_id=reversal_id, reversed=undone)


register(
    OperationSpec(
        name="finance.decision.record",
        input_model=RecordIn,
        output_model=RecordOut,
        handler=record,
        safety_class=SafetyClass.MUTATE,
        roles=frozenset({Role.OWNER, Role.SERVICE}),
        connection="owner",
        description=(
            "Record one decision with its parts and legs and a snapshot of each line"
            " it names."
        ),
    )
)
register(
    OperationSpec(
        name="finance.decision.reverse",
        input_model=ReverseIn,
        output_model=ReverseOut,
        handler=reverse,
        safety_class=SafetyClass.MUTATE,
        roles=frozenset({Role.OWNER}),
        connection="owner",
        description="Reverse one decision, or every decision of a bulk run.",
    )
)
