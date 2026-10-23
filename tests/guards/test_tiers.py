"""AC-47a: every stored field and every operation output field has a tier, and event
data holds no restricted field.

- Every column of the migrated schema has a tier (FR-46), and the shared ``MetaData``
  mirrors the migrated database, so a column a migration adds cannot dodge the check.
- A column or output field named as money, a memo, a description, a counterparty, a
  person (``owner``, the account holder's name) or an identifier
  (``RESTRICTED_NAMES`` and its prefixes and suffixes) is restricted.
- Every ``Numeric`` column and every output field holding a ``Decimal`` is restricted,
  unless ``NUMERIC_EXCEPTIONS`` or ``DECIMAL_OUTPUT_EXCEPTIONS`` names it with its
  reason (a percentage is not an amount). An exception naming nothing, or something
  already restricted, fails.
- Every field of every registered operation's output model carries a ``Tiered`` marker,
  nested models included. A restricted field covers what it holds, so a nested model
  under one is not walked.
- Every event data model (each module's ``EVENT_MODELS``, from the prompt that adds the
  event table on) marks every field, and no field of one is restricted except those
  ``contracts.EVENT_RESTRICTED_EXCEPTIONS`` lists for its type (a ``Decimal`` field or
  a restricted name counts as restricted). An exception naming a field that is not
  restricted, or a type that does not exist, fails too. Once table ``event`` is in the
  ``MetaData``, at least one non-empty ``EVENT_MODELS`` must be found, so a registry
  the scan cannot see fails instead of passing empty.
"""

from __future__ import annotations

import types
import typing
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Annotated, Any

import pytest
from pydantic import BaseModel
from sqlalchemy import Column, Connection, MetaData, Numeric, Table, Text, text

from rheo_finance import catalogue, operations
from rheo_finance.contracts import (
    EVENT_RESTRICTED_EXCEPTIONS,
    Role,
    SafetyClass,
    Tiered,
    field_tiers,
)
from rheo_finance.db import VERSION_TABLE
from rheo_finance.operations import OperationSpec
from rheo_finance.storage import tiers
from rheo_finance.storage.schema import metadata
from rheo_finance.storage.tiers import Tier, tier_of, untiered_columns

from .source_tree import event_registries

R = Tier.RESTRICTED

# --- columns --------------------------------------------------------------------------


def test_every_metadata_column_has_a_tier() -> None:
    catalogue.load_all()  # every module that defines tables is loaded
    assert untiered_columns(metadata) == []


def test_the_metadata_mirrors_every_table_in_the_database(
    owner_conn: Connection,
) -> None:
    catalogue.load_all()  # as above: a table-defining module is loaded run alone too
    in_database = {
        (table, column)
        for table, column in owner_conn.execute(
            text(
                "SELECT table_name, column_name FROM information_schema.columns"
                " WHERE table_schema = 'finance' AND table_name <> :version"
            ),
            {"version": VERSION_TABLE},
        )
    }
    in_metadata = {
        (table.name, column.name)
        for table in metadata.tables.values()
        for column in table.columns
    }
    assert in_database == in_metadata


def test_money_and_notes_are_restricted() -> None:
    for table, column in [
        ("decision", "amount"),
        ("decision", "note"),
        ("decision_item", "amount"),
        ("decision_target", "amount"),
        ("decision_target", "merchant_key"),
    ]:
        assert tier_of(table, column) is Tier.RESTRICTED, (table, column)


RESTRICTED_NAMES = frozenset(
    {
        "amount", "note", "memo", "description", "counterparty", "merchant_key",
        "identifier", "alias", "address", "txid", "cad", "total_cad", "cad_total",
        "balance", "opening_balance", "quantity", "fee", "account_number", "payee",
        "principal", "alias_norm", "owner",
    }
)  # fmt: skip
RESTRICTED_PREFIXES = (
    "description_", "counterparty_", "identifier_", "txid_", "cad_", "balance_",
    "alias_", "account_number_",
)  # fmt: skip
RESTRICTED_SUFFIXES = (
    "_amount", "_memo", "_identifier", "_address", "_txid", "_cad", "_balance",
    "_quantity", "_fee", "_principal", "_alias", "_account_number",
)  # fmt: skip

# Numeric columns that are not restricted, each with the reviewed reason.
NUMERIC_EXCEPTIONS: dict[tuple[str, str], str] = {
    ("decision", "business_pct"): "a business-use percentage, not an amount",
    ("txn", "business_pct"): "a business-use percentage, not an amount",
}
# Decimal output fields that are not restricted ("<operation> <Model>.<path>").
DECIMAL_OUTPUT_EXCEPTIONS: dict[str, str] = {}


def restricted_by_name(name: str) -> bool:
    return (
        name in RESTRICTED_NAMES
        or name.startswith(RESTRICTED_PREFIXES)
        or name.endswith(RESTRICTED_SUFFIXES)
    )


def column_name_problems(
    meta: MetaData, tier_for: Callable[[str, str], Tier | None] = tier_of
) -> list[str]:
    return sorted(
        f"{table.name}.{column.name} is {tier.value}, not restricted"
        for table in meta.tables.values()
        for column in table.columns
        if restricted_by_name(column.name)
        and (tier := tier_for(table.name, column.name)) is not None
        and tier is not R
    )


def numeric_column_problems(
    meta: MetaData,
    exceptions: Mapping[tuple[str, str], str] = NUMERIC_EXCEPTIONS,
    tier_for: Callable[[str, str], Tier | None] = tier_of,
) -> list[str]:
    problems: list[str] = []
    numeric = {
        (table.name, column.name)
        for table in meta.tables.values()
        for column in table.columns
        if isinstance(column.type, Numeric)
    }
    for table, column in sorted(numeric):
        tier = tier_for(table, column)
        if tier is not None and tier is not R and (table, column) not in exceptions:
            problems.append(f"{table}.{column} is numeric and {tier.value}")
    for (table, column), reason in sorted(exceptions.items()):
        if (table, column) not in numeric:
            problems.append(f"exception {table}.{column}: no such numeric column")
        elif tier_for(table, column) is R:
            problems.append(f"exception {table}.{column}: it is restricted already")
        if not reason.strip():
            problems.append(f"exception {table}.{column}: no reason")
    return problems


def test_every_money_memo_and_identifier_column_is_restricted() -> None:
    catalogue.load_all()
    assert column_name_problems(metadata) == []


def test_every_numeric_column_is_restricted_or_a_reviewed_exception() -> None:
    catalogue.load_all()
    assert numeric_column_problems(metadata) == []


def test_a_planted_public_numeric_column_and_a_stale_exception_are_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(tiers, "_COLUMN_TIERS", {})
    fixture = MetaData(schema="finance")
    Table(
        "planted",
        fixture,
        Column("rate", Numeric),
        Column("share_pct", Numeric),
        Column("held", Numeric),
    )
    tiers.register_column("planted", "rate", Tier.PUBLIC)
    tiers.register_column("planted", "share_pct", Tier.PUBLIC)
    tiers.register_column("planted", "held", Tier.RESTRICTED)
    exceptions = {
        ("planted", "share_pct"): "a percentage",
        ("planted", "held"): "stale",
        ("planted", "gone"): "stale",
    }
    assert numeric_column_problems(fixture, exceptions) == [
        "planted.rate is numeric and public",
        "exception planted.gone: no such numeric column",
        "exception planted.held: it is restricted already",
    ]


@pytest.mark.parametrize(
    "name",
    [
        "cad", "total_cad", "cad_total", "balance", "opening_balance", "quantity",
        "fee", "account_number", "payee", "principal", "alias_norm", "net_cad",
        "card_fee", "closing_balance",
    ],
)  # fmt: skip
def test_each_money_and_identity_shape_is_restricted_by_name(name: str) -> None:
    assert restricted_by_name(name)


@pytest.mark.parametrize(
    "name", ["fee_class", "payee_ref", "currency", "label", "count", "posting_date"]
)
def test_ordinary_names_are_not_restricted_by_name(name: str) -> None:
    assert not restricted_by_name(name)


def test_a_planted_untiered_or_public_amount_column_is_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(tiers, "_COLUMN_TIERS", {})  # planted tiers stay here
    fixture = MetaData(schema="finance")
    Table(
        "planted",
        fixture,
        Column("label", Text),
        Column("fee_amount", Numeric),
        Column("memo", Text),
    )
    tiers.register_column("planted", "label", Tier.PUBLIC)
    tiers.register_column("planted", "fee_amount", Tier.PUBLIC)
    assert untiered_columns(fixture) == [("planted", "memo")]
    assert column_name_problems(fixture) == [
        "planted.fee_amount is public, not restricted"
    ]


# --- output and event fields ----------------------------------------------------------


@dataclass(frozen=True)
class Field:
    path: str
    name: str
    tier: Tier | None
    holds_decimal: bool


def _holds_decimal(annotation: Any) -> bool:
    if annotation is Decimal:
        return True
    return any(_holds_decimal(argument) for argument in typing.get_args(annotation))


def _nested_models(annotation: Any) -> list[type[BaseModel]]:
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return [annotation]
    found: list[type[BaseModel]] = []
    origin = typing.get_origin(annotation)
    if origin is Annotated:
        return _nested_models(typing.get_args(annotation)[0])
    if origin is not None or isinstance(annotation, types.UnionType):
        for argument in typing.get_args(annotation):
            found += _nested_models(argument)
    return found


def fields_of(
    model: type[BaseModel], prefix: str = "", stack: tuple[type, ...] = ()
) -> list[Field]:
    """Every field of ``model``, and of each model it nests under a field that is not
    restricted, as dotted paths."""
    found: list[Field] = []
    marks = field_tiers(model)
    for name, info in model.model_fields.items():
        path = f"{prefix}{name}"
        found.append(Field(path, name, marks[name], _holds_decimal(info.annotation)))
        if marks[name] is R:
            continue
        for nested in _nested_models(info.annotation):
            if nested not in stack:
                found += fields_of(nested, f"{path}.", (*stack, model))
    return found


def output_problems(
    specs: Mapping[str, OperationSpec],
    exceptions: Mapping[str, str] = DECIMAL_OUTPUT_EXCEPTIONS,
) -> list[str]:
    problems: list[str] = []
    seen: set[str] = set()
    for name, spec in sorted(specs.items()):
        for field in fields_of(spec.output_model):
            key = f"{name} {spec.output_model.__name__}.{field.path}"
            seen.add(key)
            if field.tier is None:
                problems.append(f"{key} has no Tiered marker")
            elif restricted_by_name(field.name) and field.tier is not R:
                problems.append(f"{key} is {field.tier.value}, not restricted")
            elif field.holds_decimal and field.tier is not R and key not in exceptions:
                problems.append(f"{key} holds a Decimal and is {field.tier.value}")
    for key, reason in sorted(exceptions.items()):
        if key not in seen:
            problems.append(f"exception {key}: no such output field")
        if not reason.strip():
            problems.append(f"exception {key}: no reason")
    return problems


def event_problems(
    registry: Mapping[str, type[BaseModel]],
    exceptions: Mapping[str, frozenset[str]],
) -> list[str]:
    problems: list[str] = []
    restricted: dict[str, set[str]] = {}
    for event_type, model in sorted(registry.items()):
        for field in fields_of(model):
            where = f"{event_type} {model.__name__}.{field.path}"
            if field.tier is None:
                problems.append(f"{where} has no Tiered marker")
            if field.tier is R or field.holds_decimal or restricted_by_name(field.name):
                restricted.setdefault(event_type, set()).add(field.path)
                if field.path not in exceptions.get(event_type, frozenset()):
                    problems.append(f"{where} is restricted and not an exception")
    for event_type, allowed in sorted(exceptions.items()):
        if event_type not in registry:
            problems.append(f"exception for {event_type}: no such event type")
            continue
        for path in sorted(allowed - restricted.get(event_type, set())):
            problems.append(f"exception {event_type}.{path} is not a restricted field")
    return problems


def test_every_output_field_of_every_operation_carries_a_tier() -> None:
    specs = catalogue.load_all()
    assert specs, "the control: the catalogue has operations to check"
    assert output_problems(specs) == []


def test_no_event_field_is_restricted_outside_the_exceptions() -> None:
    assert event_problems(event_registries(), EVENT_RESTRICTED_EXCEPTIONS) == []


def event_registry_problems(
    meta: MetaData, registry: Mapping[str, type[BaseModel]]
) -> list[str]:
    """Fail closed: an event table with no registry the scan finds is a failure."""
    if any(table.name == "event" for table in meta.tables.values()) and not registry:
        return ["table event exists but no EVENT_MODELS registers an event type"]
    return []


def test_an_event_table_has_a_registry() -> None:
    catalogue.load_all()
    assert event_registry_problems(metadata, event_registries()) == []


def test_an_event_table_with_no_registry_found_fails() -> None:
    fixture = MetaData(schema="finance")
    Table("event", fixture, Column("type", Text))
    assert event_registry_problems(fixture, {}) == [
        "table event exists but no EVENT_MODELS registers an event type"
    ]
    assert event_registry_problems(fixture, {"finance.fee.detected": _Held}) == []
    assert event_registry_problems(MetaData(schema="finance"), {}) == []


class _Inner(BaseModel):
    label: Annotated[str, Tiered(Tier.PUBLIC)]
    hidden: str


class _Held(BaseModel):
    anything: str


class _Out(BaseModel):
    count: Annotated[int, Tiered(Tier.PUBLIC)]
    amount: Annotated[Decimal, Tiered(Tier.PUBLIC)]
    rows: Annotated[list[_Inner], Tiered(Tier.PUBLIC)]
    maybe: Annotated[_Inner | None, Tiered(Tier.INTERNAL)]
    held: Annotated[list[_Held], Tiered(R)]
    bare: int
    rate: Annotated[Decimal | None, Tiered(Tier.PUBLIC)]
    share_pct: Annotated[Decimal, Tiered(Tier.PUBLIC)]


class _Empty(BaseModel):
    pass


def _handler(_ctx: object, _uow: object, _data: _Empty) -> _Out:
    raise AssertionError("never run")


def test_a_planted_unmarked_output_field_is_found(
    empty_registry: dict[str, OperationSpec],
) -> None:
    operations.register(
        OperationSpec(
            name="finance.fixture.view",
            input_model=_Empty,
            output_model=_Out,
            handler=_handler,
            safety_class=SafetyClass.READ,
            roles=frozenset({Role.OWNER}),
            connection="owner",
            description="A fixture operation.",
        )
    )
    assert output_problems(operations.SPECS) == [
        "finance.fixture.view _Out.amount is public, not restricted",
        "finance.fixture.view _Out.rows.hidden has no Tiered marker",
        "finance.fixture.view _Out.maybe.hidden has no Tiered marker",
        "finance.fixture.view _Out.bare has no Tiered marker",
        "finance.fixture.view _Out.rate holds a Decimal and is public",
        "finance.fixture.view _Out.share_pct holds a Decimal and is public",
    ]
    reviewed = {
        "finance.fixture.view _Out.share_pct": "a percentage, not an amount",
        "finance.fixture.view _Out.gone": "stale",
    }
    problems = output_problems(operations.SPECS, reviewed)
    assert "finance.fixture.view _Out.share_pct holds a Decimal and is public" not in (
        problems
    )
    assert (
        problems[-1] == "exception finance.fixture.view _Out.gone: no such output field"
    )


class _Moved(BaseModel):
    snapshot_ref: Annotated[str, Tiered(Tier.PUBLIC)]
    total_cad: Annotated[Decimal, Tiered(R)]
    counterparty: Annotated[str, Tiered(Tier.PUBLIC)]
    pct: Annotated[Decimal, Tiered(Tier.PUBLIC)]


class _Rejected(BaseModel):
    document_ref: Annotated[str, Tiered(Tier.PUBLIC)]
    reason: str


def test_a_planted_restricted_event_field_is_found() -> None:
    registry: dict[str, type[BaseModel]] = {
        "finance.holdings.moved": _Moved,
        "finance.document.rejected": _Rejected,
    }
    assert event_problems(registry, {}) == [
        "finance.document.rejected _Rejected.reason has no Tiered marker",
        "finance.holdings.moved _Moved.total_cad is restricted and not an exception",
        "finance.holdings.moved _Moved.counterparty is restricted and not an exception",
        "finance.holdings.moved _Moved.pct is restricted and not an exception",
    ]


def test_an_exception_admits_its_field_and_a_stale_one_fails() -> None:
    registry: dict[str, type[BaseModel]] = {"finance.holdings.moved": _Moved}
    allowed = {
        "finance.holdings.moved": frozenset({"total_cad", "counterparty", "pct"})
    }
    assert event_problems(registry, allowed) == []
    stale = {
        "finance.holdings.moved": frozenset(
            {"total_cad", "counterparty", "pct", "snapshot_ref"}
        ),
        "finance.payment.detected": frozenset({"amount"}),
    }
    assert event_problems(registry, stale) == [
        "exception finance.holdings.moved.snapshot_ref is not a restricted field",
        "exception for finance.payment.detected: no such event type",
    ]
