"""The sink gate and the derived ``sink_spec`` table (FR-65, AC-78 (a), (b), (f)).

A test-only provider is registered for the ``rates`` sink; its spec has one public data
input read from the ledger on the owner connection (a count of ``finding_dismiss``
decisions), as a real provider reads its currency set. Approvals are written directly
(``record_sink_approval``) because ``finance.decision.record`` refuses them; every
write stays inside the test's rolled-back transaction. Values are synthetic.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Callable
from dataclasses import replace

import pytest
from sqlalchemy import Connection, func, select

from rheo_finance import catalogue, operations
from rheo_finance.clock import FakeClock
from rheo_finance.context import FinanceContext, UnitOfWork
from rheo_finance.contracts import Role
from rheo_finance.settings import SINKS
from rheo_finance.sinks import gate, spec
from rheo_finance.sinks.spec import (
    SINK_SPECS,
    RequestTemplate,
    SinkSpec,
    spec_digest,
)
from rheo_finance.sinks.store import write_sink_specs
from rheo_finance.storage import schema
from rheo_finance.storage.ids import uuid7

SINK = "rates"
T0 = dt.datetime(2026, 1, 15, 9, 0, tzinfo=dt.UTC)
HOUR = dt.timedelta(hours=1)


class TestSink:
    """A provider whose declaration a test can widen, as a later build would."""

    __test__ = False

    def __init__(self) -> None:
        self.hosts: tuple[str, ...] = ("rates.example.test",)
        self.requests: tuple[RequestTemplate, ...] = (
            RequestTemplate(method="GET", path="/series/:code", query_keys=("start",)),
        )

    def __call__(self, uow: UnitOfWork) -> SinkSpec:
        dismissed = uow.scalar(
            select(func.count()).where(schema.decision.c.kind == "finding_dismiss")
        )
        return SinkSpec(
            name=SINK,
            hosts=self.hosts,
            requests=self.requests,
            key_file=None,
            payload_policy_version=1,
            data_inputs=(("dismissed", str(dismissed)),),
        )


@pytest.fixture
def provider(monkeypatch: pytest.MonkeyPatch) -> TestSink:
    test_sink = TestSink()
    monkeypatch.setitem(SINK_SPECS, SINK, test_sink)
    return test_sink


@pytest.fixture
def uow(owner_conn: Connection) -> UnitOfWork:
    return UnitOfWork(owner_conn)


def digest_now(uow: UnitOfWork, provider: TestSink) -> str:
    return spec_digest(provider(uow))


# --- production registers only the rates provider; defaults are all off (AC-78a) ------


def test_only_the_rates_provider_is_registered_and_every_sink_is_disabled(
    uow: UnitOfWork,
    sink_settings: Callable[..., None],
    record_sink_approval: Callable[..., uuid.UUID],
) -> None:
    catalogue.load_all()
    assert set(spec.SINK_SPECS) == {"rates"}
    for sink in SINKS:
        assert gate.is_enabled(uow, sink) is False
    # Every setting true and an approval for each, none for the current spec: no
    # provider, or a stale approval, so still off.
    sink_settings(**dict.fromkeys(SINKS, True))
    for sink in SINKS:
        record_sink_approval(sink, "0" * 64, at=T0)
        assert gate.is_enabled(uow, sink) is False


# --- the three conditions -------------------------------------------------------------


def test_enabled_needs_the_setting_and_a_current_approval(
    uow: UnitOfWork,
    provider: TestSink,
    sink_settings: Callable[..., None],
    record_sink_approval: Callable[..., uuid.UUID],
) -> None:
    sink_settings(rates=True)
    record_sink_approval(SINK, digest_now(uow, provider), at=T0)
    assert gate.is_enabled(uow, SINK) is True


def test_a_setting_set_by_hand_with_no_approval_is_disabled(
    uow: UnitOfWork, provider: TestSink, sink_settings: Callable[..., None]
) -> None:
    sink_settings(rates=True)
    assert gate.is_enabled(uow, SINK) is False


def test_an_approval_with_the_setting_off_is_disabled(
    uow: UnitOfWork,
    provider: TestSink,
    sink_settings: Callable[..., None],
    record_sink_approval: Callable[..., uuid.UUID],
) -> None:
    sink_settings(rates=False)
    record_sink_approval(SINK, digest_now(uow, provider), at=T0)
    assert gate.is_enabled(uow, SINK) is False


def test_an_approval_for_another_sink_does_not_enable_this_one(
    uow: UnitOfWork,
    provider: TestSink,
    sink_settings: Callable[..., None],
    record_sink_approval: Callable[..., uuid.UUID],
) -> None:
    sink_settings(rates=True)
    record_sink_approval("coinstats", digest_now(uow, provider), at=T0)
    assert gate.is_enabled(uow, SINK) is False


# --- staleness (AC-78f) ---------------------------------------------------------------


def widen_host(provider: TestSink, _insert: Callable[..., uuid.UUID]) -> None:
    provider.hosts = (*provider.hosts, "more.example.test")


def widen_request(provider: TestSink, _insert: Callable[..., uuid.UUID]) -> None:
    provider.requests = (
        *provider.requests,
        RequestTemplate(method="GET", path="/latest"),
    )


def widen_query(provider: TestSink, _insert: Callable[..., uuid.UUID]) -> None:
    provider.requests = (
        RequestTemplate(
            method="GET", path="/series/:code", query_keys=("start", "end")
        ),
    )


def change_data_input(provider: TestSink, insert: Callable[..., uuid.UUID]) -> None:
    ref = f"finance.document:{uuid7(T0)}"
    insert("finding_dismiss", at=T0, target_ref=ref)


@pytest.mark.parametrize(
    "change", [widen_host, widen_request, widen_query, change_data_input]
)
def test_a_changed_spec_makes_the_approval_stale(
    change: Callable[[TestSink, Callable[..., uuid.UUID]], None],
    uow: UnitOfWork,
    provider: TestSink,
    sink_settings: Callable[..., None],
    record_sink_approval: Callable[..., uuid.UUID],
    insert_decision: Callable[..., uuid.UUID],
) -> None:
    sink_settings(rates=True)
    record_sink_approval(SINK, digest_now(uow, provider), at=T0)
    assert gate.is_enabled(uow, SINK) is True
    change(provider, insert_decision)
    assert gate.is_enabled(uow, SINK) is False
    # A fresh approval of the widened spec enables it again.
    record_sink_approval(SINK, digest_now(uow, provider), at=T0 + HOUR)
    assert gate.is_enabled(uow, SINK) is True


def test_declaration_order_does_not_change_the_digest() -> None:
    first = RequestTemplate(method="GET", path="/a", query_keys=("x", "y"))
    second = RequestTemplate(method="POST", path="/b")
    one = SinkSpec(
        name=SINK, hosts=("a.example.test", "b.example.test"),
        requests=(first, second), key_file="rates.key", payload_policy_version=2,
        data_inputs=(("one", "1"), ("two", "2")),
    )  # fmt: skip
    other = SinkSpec(
        name=SINK, hosts=("b.example.test", "a.example.test"),
        requests=(second, first.model_copy(update={"query_keys": ("x", "y")})),
        key_file="rates.key", payload_policy_version=2,
        data_inputs=(("two", "2"), ("one", "1")),
    )  # fmt: skip
    assert spec_digest(one) == spec_digest(other)
    assert spec_digest(one) != spec_digest(one.model_copy(update={"key_file": None}))
    bumped = one.model_copy(update={"payload_policy_version": 3})
    assert spec_digest(one) != spec_digest(bumped)


# --- disable and reverse --------------------------------------------------------------


@pytest.fixture
def owner(rolled_back_context: Callable[[Role], FinanceContext]) -> FinanceContext:
    return replace(rolled_back_context(Role.OWNER), clock=FakeClock(T0 + HOUR))


def test_a_newer_disable_turns_the_sink_off_until_a_newer_approval(
    uow: UnitOfWork,
    owner: FinanceContext,
    provider: TestSink,
    sink_settings: Callable[..., None],
    record_sink_approval: Callable[..., uuid.UUID],
) -> None:
    sink_settings(rates=True)
    record_sink_approval(SINK, digest_now(uow, provider), at=T0)
    operations.dispatch(
        "finance.decision.record",
        {"kind": "sink_disable", "sink": SINK},
        Role.OWNER,
        ctx=owner,
    )  # recorded at T0 + 1h
    assert gate.is_enabled(uow, SINK) is False
    record_sink_approval(SINK, digest_now(uow, provider), at=T0 + 2 * HOUR)
    assert gate.is_enabled(uow, SINK) is True


def test_a_reversed_approval_is_not_an_approval(
    uow: UnitOfWork,
    owner: FinanceContext,
    provider: TestSink,
    sink_settings: Callable[..., None],
    record_sink_approval: Callable[..., uuid.UUID],
) -> None:
    sink_settings(rates=True)
    approval = record_sink_approval(SINK, digest_now(uow, provider), at=T0)
    operations.dispatch(
        "finance.decision.reverse", {"decision_id": approval}, Role.OWNER, ctx=owner
    )
    assert gate.is_enabled(uow, SINK) is False


def test_reversing_the_newer_of_two_approvals_does_not_fall_back_to_the_older(
    uow: UnitOfWork,
    owner: FinanceContext,
    provider: TestSink,
    sink_settings: Callable[..., None],
    record_sink_approval: Callable[..., uuid.UUID],
) -> None:
    sink_settings(rates=True)
    digest = digest_now(uow, provider)
    record_sink_approval(SINK, digest, at=T0 - HOUR)
    newer = record_sink_approval(SINK, digest, at=T0)
    assert gate.is_enabled(uow, SINK) is True
    operations.dispatch(  # recorded at T0 + 1h
        "finance.decision.reverse", {"decision_id": newer}, Role.OWNER, ctx=owner
    )
    assert gate.is_enabled(uow, SINK) is False
    # Only a fresh approval turns it on again.
    record_sink_approval(SINK, digest, at=T0 + 2 * HOUR)
    assert gate.is_enabled(uow, SINK) is True


# --- the derived sink_spec table ------------------------------------------------------


def stored(conn: Connection) -> dict[str, dict[str, object]]:
    rows = conn.execute(select(schema.sink_spec)).mappings()
    return {row["sink"]: dict(row) for row in rows}


def test_write_sink_specs_stores_the_digest_the_gate_computes(
    owner_conn: Connection,
    uow: UnitOfWork,
    provider: TestSink,
    sink_settings: Callable[..., None],
    record_sink_approval: Callable[..., uuid.UUID],
    insert_decision: Callable[..., uuid.UUID],
) -> None:
    write_sink_specs(uow)
    rows = stored(owner_conn)
    assert set(rows) == {SINK}
    row = rows[SINK]
    assert row["spec_digest"] == digest_now(uow, provider)
    assert row["hosts"] == "rates.example.test"
    assert row["request_templates"] == "GET /series/:code ?start"
    assert row["key_file"] is None
    assert row["policy_version"] == 1
    assert row["data_inputs"] == "dismissed=0"
    assert row["computed_at"] is not None

    # An approval of the stored digest is what the gate accepts.
    sink_settings(rates=True)
    record_sink_approval(SINK, str(row["spec_digest"]), at=T0)
    assert gate.is_enabled(uow, SINK) is True

    # A data change rewrites the row; the stored digest follows the gate's.
    change_data_input(provider, insert_decision)
    write_sink_specs(uow)
    again = stored(owner_conn)[SINK]
    assert again["data_inputs"] == "dismissed=1"
    assert again["spec_digest"] == digest_now(uow, provider) != row["spec_digest"]
    assert gate.is_enabled(uow, SINK) is False


def test_write_sink_specs_drops_a_sink_with_no_provider(
    owner_conn: Connection,
    uow: UnitOfWork,
    provider: TestSink,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_sink_specs(uow)
    assert set(stored(owner_conn)) == {SINK}
    monkeypatch.delitem(SINK_SPECS, SINK)
    write_sink_specs(uow)
    assert stored(owner_conn) == {}


def test_a_provider_for_another_sink_is_an_error(
    uow: UnitOfWork, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(SINK_SPECS, "coinstats", TestSink())
    with pytest.raises(ValueError, match="another sink"):
        write_sink_specs(uow)
