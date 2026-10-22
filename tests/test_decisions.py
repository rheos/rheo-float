"""Decisions: record, reverse, the append-only trigger and the per-kind CHECK.

Every write goes through ``dispatch`` inside the test's rolled-back transaction
(``rolled_back_context``), because the append-only trigger would keep a committed row
in ``finance_test`` for good. Values are synthetic.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Callable, Mapping
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from psycopg import errors as pg_errors
from sqlalchemy import Connection, func, insert, select, text
from sqlalchemy.exc import DBAPIError, IntegrityError

from rheo_finance import composition, db, decision, operations
from rheo_finance.cli.main import main
from rheo_finance.clock import FakeClock
from rheo_finance.context import FinanceContext, LineSnapshot, UnitOfWork
from rheo_finance.contracts import Role
from rheo_finance.decision import (
    DECISION_KIND_COLUMNS,
    MATRIX_COLUMNS,
    DecisionKind,
    DecisionRefused,
    MerchantKey,
)
from rheo_finance.operations import RoleRefused, dispatch
from rheo_finance.storage import schema
from rheo_finance.storage.ids import uuid7

K = DecisionKind
START = dt.datetime(2026, 1, 15, 12, 0, tzinfo=dt.UTC)
MakeContext = Callable[[Role], FinanceContext]


def line_ref() -> str:
    return f"finance.txn:{uuid7(START)}"


LINE_A, LINE_B, LINE_C, LINE_D = (line_ref() for _ in range(4))
UNKNOWN_LINE = line_ref()
OBLIGATION = f"finance.obligation:{uuid7(START)}"
SNAPSHOTS = {
    ref: LineSnapshot(
        account_slug=f"chequing-999{index}",
        amount=Decimal(f"-{index}9.10"),
        currency="CAD",
        posting_date=dt.date(2026, 1, 10 + index),
    )
    for index, ref in enumerate((LINE_A, LINE_B, LINE_C, LINE_D), start=1)
}


class Calls:
    def __init__(self) -> None:
        self.refs: list[str] = []

    def provider(self, _uow: UnitOfWork, ref: str) -> LineSnapshot | None:
        self.refs.append(ref)
        return SNAPSHOTS.get(ref)


@pytest.fixture
def calls() -> Calls:
    return Calls()


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(START)


@pytest.fixture
def owner(
    rolled_back_context: MakeContext, calls: Calls, clock: FakeClock
) -> FinanceContext:
    return replace(
        rolled_back_context(Role.OWNER), snapshot_provider=calls.provider, clock=clock
    )


def record(ctx: FinanceContext, **fields: Any) -> decision.RecordOut:
    out = dispatch("finance.decision.record", fields, ctx.role, ctx=ctx)
    assert isinstance(out, decision.RecordOut)
    assert isinstance(ctx.clock, FakeClock)
    ctx.clock.advance(dt.timedelta(minutes=1))  # so "newest" is never a tie
    return out


def reverse(ctx: FinanceContext, **fields: Any) -> decision.ReverseOut:
    out = dispatch("finance.decision.reverse", fields, Role.OWNER, ctx=ctx)
    assert isinstance(out, decision.ReverseOut)
    assert isinstance(ctx.clock, FakeClock)
    ctx.clock.advance(dt.timedelta(minutes=1))
    return out


def rows(conn: Connection, table: Any, decision_id: uuid.UUID) -> list[dict[str, Any]]:
    key = table.c.id if table is schema.decision else table.c.decision_id
    order = [table.c.id] if table is schema.decision else [table.c.seq]
    found = conn.execute(select(table).where(key == decision_id).order_by(*order))
    return [dict(row) for row in found.mappings()]


def count(conn: Connection, table: Any) -> int:
    return int(conn.execute(select(func.count()).select_from(table)).scalar_one())


def effective(
    conn: Connection, kind: Any, target: str | uuid.UUID | MerchantKey
) -> uuid.UUID | None:
    found = decision.effective(UnitOfWork(conn), kind, target)
    return None if found is None else found.id


# --- finance.decision.record ----------------------------------------------------------


def test_record_writes_the_decision_its_parts_and_the_line_snapshot(
    owner: FinanceContext, owner_conn: Connection, calls: Calls
) -> None:
    out = record(
        owner,
        kind="split",
        target_ref=LINE_A,
        note="synthetic note",
        parts=[
            {"label": "rent", "amount": "900.00", "covers_ref": "rent:2026-01"},
            {"label": "usdt sale proceeds", "amount": "100.00"},
        ],
    )
    assert (out.items, out.targets) == (2, 1)
    [row] = rows(owner_conn, schema.decision, out.decision_id)
    assert row["kind"] == "split" and row["target_ref"] == LINE_A
    assert row["decided_at"] == START and row["note"] == "synthetic note"
    parts = rows(owner_conn, schema.decision_item, out.decision_id)
    assert [(p["seq"], p["role"], p["label"], p["amount"]) for p in parts] == [
        (1, "part", "rent", Decimal("900.00")),
        (2, "part", "usdt sale proceeds", Decimal("100.00")),
    ]
    assert parts[0]["covers_ref"] == "rent:2026-01"
    [target] = rows(owner_conn, schema.decision_target, out.decision_id)
    snapshot = SNAPSHOTS[LINE_A]
    assert target == {
        "decision_id": out.decision_id,
        "seq": 1,
        "target_ref": LINE_A,
        "account_slug": snapshot.account_slug,
        "amount": snapshot.amount,
        "currency": snapshot.currency,
        "posting_date": snapshot.posting_date,
        "merchant_key": None,
    }
    assert calls.refs == [LINE_A]


def test_a_pair_snapshots_the_target_the_counterpart_and_each_leg(
    owner: FinanceContext, owner_conn: Connection
) -> None:
    out = record(
        owner,
        kind="pair",
        target_ref=LINE_A,
        counterpart_ref=LINE_B,
        legs=[{"target_ref": LINE_C, "amount": "1.50"}, {"target_ref": LINE_B}],
    )
    assert (out.items, out.targets) == (2, 3)
    legs = rows(owner_conn, schema.decision_item, out.decision_id)
    assert [(leg["role"], leg["target_ref"]) for leg in legs] == [
        ("leg", LINE_C),
        ("leg", LINE_B),
    ]
    targets = rows(owner_conn, schema.decision_target, out.decision_id)
    assert [t["target_ref"] for t in targets] == [LINE_A, LINE_B, LINE_C]
    assert [t["account_slug"] for t in targets] == [
        SNAPSHOTS[ref].account_slug for ref in (LINE_A, LINE_B, LINE_C)
    ]


def test_a_merchant_category_snapshots_the_newest_line_with_its_key(
    owner: FinanceContext, owner_conn: Connection
) -> None:
    out = record(
        owner,
        kind="category",
        merchant_key="grocer 999",
        newest_line_ref=LINE_D,
        category="groceries",
    )
    [row] = rows(owner_conn, schema.decision, out.decision_id)
    assert row["target_ref"] is None and row["category"] == "groceries"
    [target] = rows(owner_conn, schema.decision_target, out.decision_id)
    assert (target["target_ref"], target["merchant_key"]) == (LINE_D, "grocer 999")
    assert target["amount"] == SNAPSHOTS[LINE_D].amount
    # The merchant key finds it; the snapshot line does not (a snapshot never
    # decides where a decision applies).
    assert effective(owner_conn, K.CATEGORY, MerchantKey("grocer 999")) == (
        out.decision_id
    )
    assert effective(owner_conn, K.CATEGORY, LINE_D) is None
    assert effective(owner_conn, K.CATEGORY, MerchantKey("grocer 998")) is None


def test_a_line_that_resolves_to_no_current_line_is_refused(
    owner: FinanceContext, owner_conn: Connection
) -> None:
    with pytest.raises(DecisionRefused, match="counterpart_ref resolves to no"):
        record(owner, kind="pair", target_ref=LINE_A, counterpart_ref=UNKNOWN_LINE)
    assert count(owner_conn, schema.decision) == 0
    assert count(owner_conn, schema.decision_target) == 0


def test_a_decision_on_no_line_asks_the_provider_nothing(
    owner: FinanceContext, owner_conn: Connection, calls: Calls
) -> None:
    out = record(owner, kind="presumed_paid", obligation_ref=OBLIGATION)
    assert (out.items, out.targets) == (0, 0)
    assert calls.refs == []
    assert effective(owner_conn, K.PRESUMED_PAID, OBLIGATION) == out.decision_id


@pytest.mark.parametrize(
    ("fields", "message"),
    [
        ({"kind": "pair"}, "pair: needs target_ref"),
        ({"kind": "sink_disable", "sink": "rates", "category": "x"}, "does not use"),
        ({"kind": "category", "target_ref": LINE_A}, "at least one of category, tag"),
        (
            {"kind": "category", "target_ref": LINE_A, "category": "x",
             "merchant_key": "m", "newest_line_ref": LINE_B},
            "exactly one of target_ref and merchant_key",
        ),
        (
            {"kind": "category", "merchant_key": "m", "category": "x"},
            "comes with the merchant's newest_line_ref",
        ),
        ({"kind": "pair", "target_ref": LINE_A, "merchant_key": "m"}, "merchant_key"),
        (
            {"kind": "pair", "target_ref": LINE_A,
             "parts": [{"label": "rent", "amount": "1"}]},
            "does not take parts",
        ),
        ({"kind": "split", "target_ref": LINE_A}, "at least one part"),
        (
            {"kind": "split", "target_ref": LINE_A, "legs": [{"target_ref": LINE_B}],
             "parts": [{"label": "rent", "amount": "1"}]},
            "does not take legs",
        ),
        (
            {"kind": "sink_approve", "sink": "rates", "spec_digest": "a",
             "preview_digest": "b"},
            "recorded only by finance sink approve",
        ),
        ({"kind": "reverse"}, "recorded only by finance.decision.reverse"),
        (
            {"kind": "include", "target_ref": LINE_A,
             "about_decision_id": str(uuid7(START))},
            "about_decision_id names no decision",
        ),
    ],
)  # fmt: skip
def test_record_refuses_what_the_kind_does_not_allow(
    owner: FinanceContext, owner_conn: Connection, fields: dict[str, Any], message: str
) -> None:
    with pytest.raises(DecisionRefused, match=message):
        record(owner, **fields)
    assert count(owner_conn, schema.decision) == 0


@pytest.mark.parametrize(
    "fields",
    [
        {"kind": "pair", "target_ref": "finance.txn:not-a-uuid"},
        {"kind": "finding_dismiss", "target_ref": LINE_A, "amount": 1.5},
        {"kind": "category", "target_ref": LINE_A, "tag": "split",
         "business_pct": "101"},
        {"kind": "split", "target_ref": LINE_A,
         "parts": [{"label": "rent", "amount": "1", "covers_ref": "rent:2026-13"}]},
        {"kind": "nonsense", "target_ref": LINE_A},
    ],
)  # fmt: skip
def test_record_refuses_a_malformed_input(
    owner: FinanceContext, fields: dict[str, Any]
) -> None:
    with pytest.raises(operations.InputInvalid):
        record(owner, **fields)


def test_a_reattachment_has_the_orphans_kind(
    owner: FinanceContext, owner_conn: Connection
) -> None:
    orphan = record(owner, kind="include", target_ref=LINE_A)
    with pytest.raises(DecisionRefused, match="the orphan's own kind"):
        record(owner, kind="unpair", target_ref=LINE_B,
               about_decision_id=str(orphan.decision_id))  # fmt: skip
    again = record(
        owner, kind="include", target_ref=LINE_B, about_decision_id=orphan.decision_id
    )
    decline = record(
        owner, kind="reattach_decline", about_decision_id=orphan.decision_id,
        target_ref=LINE_C,
    )  # fmt: skip
    assert effective(owner_conn, K.INCLUDE, LINE_B) == again.decision_id
    assert effective(owner_conn, K.REATTACH_DECLINE, orphan.decision_id) == (
        decline.decision_id
    )


def test_the_service_role_records_and_a_member_is_refused(
    rolled_back_context: MakeContext, calls: Calls, owner_conn: Connection
) -> None:
    service = replace(
        rolled_back_context(Role.SERVICE), snapshot_provider=calls.provider
    )
    out = dispatch(
        "finance.decision.record",
        {"kind": "include", "target_ref": LINE_A},
        Role.SERVICE,
        ctx=service,
    )
    assert isinstance(out, decision.RecordOut)
    with pytest.raises(RoleRefused):
        dispatch("finance.decision.reverse", {"decision_id": str(out.decision_id)},
                 Role.SERVICE, ctx=service)  # fmt: skip
    member = rolled_back_context(Role.MEMBER)
    with pytest.raises(RoleRefused):
        dispatch("finance.decision.record", {"kind": "include", "target_ref": LINE_A},
                 Role.MEMBER, ctx=member)  # fmt: skip


# --- the newest unreversed decision ---------------------------------------------------


def test_effective_is_the_newest_decision_and_falls_back_on_reversal(
    owner: FinanceContext, owner_conn: Connection
) -> None:
    older = record(owner, kind="category", target_ref=LINE_A, category="dining")
    newer = record(owner, kind="category", target_ref=LINE_A, category="groceries")
    record(owner, kind="category", target_ref=LINE_B, category="fuel")
    assert effective(owner_conn, K.CATEGORY, LINE_A) == newer.decision_id
    assert effective(owner_conn, K.INCLUDE, LINE_A) is None

    out = reverse(owner, decision_id=str(newer.decision_id), note="synthetic")
    assert out.reversed == 1
    [row] = rows(owner_conn, schema.decision, out.decision_id)
    assert (row["kind"], row["reverses_id"], row["reverses_bulk_id"]) == (
        "reverse", newer.decision_id, None,
    )  # fmt: skip
    # History stays: the reversed decision is still there, it just stops applying.
    assert rows(owner_conn, schema.decision, newer.decision_id) != []
    assert effective(owner_conn, K.CATEGORY, LINE_A) == older.decision_id
    reverse(owner, decision_id=str(older.decision_id))
    assert effective(owner_conn, K.CATEGORY, LINE_A) is None


def test_effective_over_two_kinds_sharing_a_target(
    owner: FinanceContext, owner_conn: Connection
) -> None:
    disable = record(owner, kind="sink_disable", sink="rates")
    kinds = (K.SINK_APPROVE, K.SINK_DISABLE)
    assert effective(owner_conn, kinds, "rates") == disable.decision_id
    assert effective(owner_conn, kinds, "imap") is None
    with pytest.raises(ValueError, match="share one target column"):
        effective(owner_conn, (K.SINK_DISABLE, K.CATEGORY), "rates")
    with pytest.raises(ValueError, match="no target"):
        effective(owner_conn, K.REVERSE, "rates")


def test_reverse_refuses_a_second_reversal_a_reversal_and_an_unknown_id(
    owner: FinanceContext, owner_conn: Connection
) -> None:
    made = record(owner, kind="include", target_ref=LINE_A)
    out = reverse(owner, decision_id=str(made.decision_id))
    with pytest.raises(DecisionRefused, match="already reversed"):
        reverse(owner, decision_id=str(made.decision_id))
    with pytest.raises(DecisionRefused, match="a reversal is not reversed"):
        reverse(owner, decision_id=str(out.decision_id))
    with pytest.raises(DecisionRefused, match="no decision has that id"):
        reverse(owner, decision_id=str(uuid7(START)))
    with pytest.raises(DecisionRefused, match="exactly one of"):
        reverse(owner)
    with pytest.raises(DecisionRefused, match="exactly one of"):
        reverse(owner, decision_id=str(made.decision_id), bulk_id=str(uuid7(START)))
    assert count(owner_conn, schema.decision) == 2


def test_reverse_by_bulk_id_un_applies_every_decision_of_the_run(
    owner: FinanceContext, owner_conn: Connection
) -> None:
    bulk = uuid7(START)
    before = record(owner, kind="category", target_ref=LINE_A, category="dining")
    in_bulk = {
        ref: record(owner, kind="category", target_ref=ref, category="groceries",
                    bulk_id=str(bulk))
        for ref in (LINE_A, LINE_B, LINE_C)
    }  # fmt: skip
    other_bulk = record(
        owner, kind="category", target_ref=LINE_D, category="fuel",
        bulk_id=str(uuid7(START)),
    )  # fmt: skip
    for ref, made in in_bulk.items():
        assert effective(owner_conn, K.CATEGORY, ref) == made.decision_id
    reverse(owner, decision_id=str(in_bulk[LINE_C].decision_id))

    out = reverse(owner, bulk_id=str(bulk))
    assert out.reversed == 2  # LINE_C's was already reversed on its own
    [row] = rows(owner_conn, schema.decision, out.decision_id)
    assert (row["reverses_id"], row["reverses_bulk_id"]) == (None, bulk)
    assert effective(owner_conn, K.CATEGORY, LINE_A) == before.decision_id
    assert effective(owner_conn, K.CATEGORY, LINE_B) is None
    assert effective(owner_conn, K.CATEGORY, LINE_C) is None
    assert effective(owner_conn, K.CATEGORY, LINE_D) == other_bulk.decision_id

    with pytest.raises(DecisionRefused, match="bulk run is already reversed"):
        reverse(owner, bulk_id=str(bulk))
    with pytest.raises(DecisionRefused, match="already reversed"):
        reverse(owner, decision_id=str(in_bulk[LINE_B].decision_id))
    with pytest.raises(DecisionRefused, match="no decision has that bulk id"):
        reverse(owner, bulk_id=str(uuid7(START)))
    # A decision cannot join a reversed run: it would never apply.
    with pytest.raises(DecisionRefused, match="bulk run is already reversed"):
        record(owner, kind="category", target_ref=LINE_B, category="fuel",
               bulk_id=str(bulk))  # fmt: skip
    assert effective(owner_conn, K.CATEGORY, LINE_B) is None


# --- the append-only trigger ----------------------------------------------------------


@pytest.fixture
def written(owner: FinanceContext) -> decision.RecordOut:
    """One decision with an item and a snapshot row, so every table has a row."""
    return record(
        owner, kind="split", target_ref=LINE_A,
        parts=[{"label": "rent", "amount": "10.00"}],
    )  # fmt: skip


@pytest.mark.parametrize("table", ["decision", "decision_item", "decision_target"])
@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE finance.{table} SET {column} = {column}",
        "DELETE FROM finance.{table}",
        "TRUNCATE finance.{table} CASCADE",
    ],
)
def test_update_delete_and_truncate_are_refused(
    owner_conn: Connection, written: decision.RecordOut, table: str, statement: str
) -> None:
    column = "decision_id" if table != "decision" else "id"
    savepoint = owner_conn.begin_nested()
    with pytest.raises(DBAPIError) as caught:
        owner_conn.execute(text(statement.format(table=table, column=column)))
    savepoint.rollback()
    assert isinstance(caught.value.orig, pg_errors.RaiseException)
    assert "append-only" in str(caught.value.orig)
    assert count(owner_conn, schema.metadata.tables[f"finance.{table}"]) >= 1


# --- the per-kind CHECK, against the code matrix --------------------------------------


@pytest.fixture
def base_decision(written: decision.RecordOut) -> uuid.UUID:
    return written.decision_id


def column_values(base: uuid.UUID) -> dict[str, object]:
    return {
        "target_ref": LINE_A,
        "category": "groceries",
        "tag": "personal",
        "business_unit": "studio",
        "business_pct": Decimal("50"),
        "counterpart_ref": LINE_B,
        "obligation_ref": OBLIGATION,
        "party_ref": f"relationships.party:{uuid.uuid4()}",
        "account_ref": f"finance.account:{uuid7(START)}",
        "effective_on": dt.date(2026, 1, 31),
        "sink": "rates",
        "spec_digest": "spec",
        "preview_digest": "preview",
        "amount": Decimal("12.34"),
        "about_decision_id": base,
        "bulk_id": uuid7(START),
        "reverses_id": base,
        "reverses_bulk_id": uuid7(START),
    }


def accepted(conn: Connection, kind: str, columns: Mapping[str, object]) -> bool:
    """Whether the database takes the row; every attempt is rolled back."""
    savepoint = conn.begin_nested()
    try:
        conn.execute(
            insert(schema.decision).values(
                id=uuid7(START), kind=kind, decided_at=START, **columns
            )
        )
        return True
    except IntegrityError as exc:
        assert isinstance(exc.orig, pg_errors.CheckViolation), exc.orig
        return False
    finally:
        savepoint.rollback()


def minimal(kind: K, values: dict[str, object]) -> dict[str, object]:
    rules = DECISION_KIND_COLUMNS[kind]
    names = set(rules.required)
    names |= {sorted(group)[0] for group in rules.exactly_one + rules.at_least_one}
    return {name: values[name] for name in names}


@pytest.fixture
def matrix_conn(
    owner_conn: Connection, base_decision: uuid.UUID
) -> tuple[Connection, dict[str, object]]:
    return owner_conn, column_values(base_decision)


@pytest.mark.parametrize("kind", list(DecisionKind))
def test_the_database_check_matches_the_code_matrix(
    matrix_conn: tuple[Connection, dict[str, object]], kind: DecisionKind
) -> None:
    conn, values = matrix_conn
    rules = DECISION_KIND_COLUMNS[kind]
    row = minimal(kind, values)
    assert accepted(conn, kind.value, row), "the minimal row is refused"
    exclusive = frozenset().union(*rules.exactly_one)
    for column in MATRIX_COLUMNS:
        if column in row:
            continue
        expected = column in rules.allowed and column not in exclusive
        got = accepted(conn, kind.value, {**row, column: values[column]})
        assert got is expected, f"{kind.value} + {column}"
    for column in rules.required:
        without = {name: value for name, value in row.items() if name != column}
        assert not accepted(conn, kind.value, without), f"{kind.value} - {column}"
    for group in rules.exactly_one + rules.at_least_one:
        without = {name: value for name, value in row.items() if name not in group}
        assert not accepted(conn, kind.value, without), f"{kind.value} - {group}"


def test_sink_approve_without_its_digests_is_refused_by_the_database(
    matrix_conn: tuple[Connection, dict[str, object]],
) -> None:
    conn, _ = matrix_conn
    full = {"sink": "rates", "spec_digest": "spec", "preview_digest": "preview"}
    assert accepted(conn, "sink_approve", full)
    for missing in ("spec_digest", "preview_digest", "sink"):
        row = {name: value for name, value in full.items() if name != missing}
        assert not accepted(conn, "sink_approve", row), missing


def test_reverse_needs_exactly_one_target_in_the_database(
    matrix_conn: tuple[Connection, dict[str, object]],
) -> None:
    conn, values = matrix_conn
    by_id = {"reverses_id": values["reverses_id"]}
    by_bulk = {"reverses_bulk_id": values["reverses_bulk_id"]}
    assert accepted(conn, "reverse", by_id)
    assert accepted(conn, "reverse", by_bulk)
    assert not accepted(conn, "reverse", {})
    assert not accepted(conn, "reverse", {**by_id, **by_bulk})
    assert not accepted(conn, "no_such_kind", by_id)


@pytest.mark.parametrize(
    "item",
    [
        {"role": "part", "amount": Decimal("1")},
        {"role": "part", "label": "rent"},
        {"role": "part", "label": "rent", "amount": Decimal("1"), "target_ref": LINE_A},
        {"role": "leg"},
        {"role": "leg", "target_ref": LINE_A, "label": "rent"},
        {"role": "other", "target_ref": LINE_A},
    ],
)
def test_the_database_refuses_a_malformed_item(
    owner_conn: Connection, base_decision: uuid.UUID, item: dict[str, object]
) -> None:
    savepoint = owner_conn.begin_nested()
    with pytest.raises(IntegrityError) as caught:
        owner_conn.execute(
            insert(schema.decision_item).values(
                decision_id=base_decision, seq=9, **item
            )
        )
    savepoint.rollback()
    assert isinstance(caught.value.orig, pg_errors.CheckViolation)


# --- finance decision reverse ---------------------------------------------------------


@pytest.fixture
def cli_context(
    owner: FinanceContext, monkeypatch: pytest.MonkeyPatch
) -> FinanceContext:
    monkeypatch.setattr(composition, "build_context", lambda role: owner)
    return owner


def test_cli_reverses_a_decision_by_id(
    cli_context: FinanceContext,
    owner_conn: Connection,
    capsys: pytest.CaptureFixture[str],
) -> None:
    made = record(cli_context, kind="include", target_ref=LINE_A)
    assert main(["decision", "reverse", str(made.decision_id)]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[0] == (
        f"finance decision reverse: reversed decision {made.decision_id}."
    )
    assert lines[1].startswith("Recorded as decision ")
    assert effective(owner_conn, K.INCLUDE, LINE_A) is None


def test_cli_reverses_a_bulk_run(
    cli_context: FinanceContext,
    owner_conn: Connection,
    capsys: pytest.CaptureFixture[str],
) -> None:
    bulk = uuid7(START)
    for ref in (LINE_A, LINE_B):
        record(cli_context, kind="include", target_ref=ref, bulk_id=str(bulk))
    assert main(["decision", "reverse", "--bulk", str(bulk)]) == 0
    out = capsys.readouterr().out
    assert out.startswith(
        f"finance decision reverse: reversed bulk run {bulk};"
        " 2 decision(s) no longer apply."
    )
    assert effective(owner_conn, K.INCLUDE, LINE_A) is None
    assert effective(owner_conn, K.INCLUDE, LINE_B) is None


def test_cli_reports_a_refusal_and_exits_one(
    cli_context: FinanceContext, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["decision", "reverse", str(uuid7(START))]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.startswith(
        "finance decision reverse: refused. reverse: no decision has that id"
    )


@pytest.mark.parametrize(
    "argv",
    [
        ["decision", "reverse"],
        ["decision", "reverse", str(uuid7(START)), "--bulk", str(uuid7(START))],
        ["decision", "reverse", "not-a-uuid"],
        ["decision"],
    ],
)
def test_cli_usage_errors_exit_two(
    argv: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(argv) == 2
    assert capsys.readouterr().out == ""


def test_the_production_context_reads_db_env_and_opens_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / db.ENV_FILE_NAME).write_text(
        f"{db.OWNER_DSN_KEY}=postgresql://finance_owner:pw@127.0.0.1:1/finance\n"
        f"{db.READER_DSN_KEY}=postgresql://finance_reader:pw@127.0.0.1:1/finance\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("FINANCE_DATA_DIR", str(tmp_path))
    ctx = composition.build_context(Role.OWNER)
    assert ctx.role is Role.OWNER
    assert ctx.paths.data == tmp_path
    assert ctx._databases is not None
    assert ctx.snapshot_provider is composition.no_current_lines
