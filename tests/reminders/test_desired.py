"""Public reminder seams over invented rows in rolled-back finance_test transactions."""

from __future__ import annotations

import dataclasses
import datetime as dt
from collections.abc import Callable
from decimal import Decimal

import pytest
from pydantic import ValidationError
from sqlalchemy import Connection, insert, select

from derive.support import Ledger
from derive.test_payment_application import (
    N1,
    N,
    card_export,
    paid_card,
    statement_n,
    statement_n1,
    with_settings,
)
from rheo_finance import catalogue, operations
from rheo_finance.clock import FakeClock
from rheo_finance.context import FinanceContext
from rheo_finance.contracts import Role, SafetyClass
from rheo_finance.operations import OperationError, RoleRefused, dispatch
from rheo_finance.reminders.desired import DesiredOut
from rheo_finance.reminders.policy import PayloadPolicy, PayloadRefused, build_payload
from rheo_finance.settings import parse_settings
from rheo_finance.storage.ids import uuid7
from rheo_finance.storage.schema import obligation, payee

D = dt.date
M = Decimal
NOW = dt.datetime(2026, 5, 1, 12, tzinfo=dt.UTC)
DUE = D(2026, 5, 20)
FLOOR = '[finance.reminders]\nmin_due_date = "2026-01-01"\n'


def context(
    build: Callable[[Role], FinanceContext],
    *,
    role: Role = Role.OWNER,
    settings: str = FLOOR,
    when: dt.datetime = NOW,
) -> FinanceContext:
    return dataclasses.replace(
        build(role), settings=parse_settings(settings), clock=FakeClock(when)
    )


def desired(ctx: FinanceContext) -> DesiredOut:
    catalogue.load_all()
    out = dispatch("finance.reminder.desired", {}, ctx.role, ctx=ctx)
    assert isinstance(out, DesiredOut)
    return out


def add(
    conn: Connection,
    *,
    period: str = "bill-2026-05",
    due: dt.date | None = DUE,
    status: str = "open",
    before_floor: bool = False,
    threshold: str = "40.00",
    kind: str = "bill",
    label: str = "Sample Utility",
    slug: str = "sample-utility",
) -> None:
    pid = conn.scalar(select(payee.c.id).where(payee.c.slug == slug))
    if pid is None:
        pid = uuid7(NOW)
        conn.execute(
            insert(payee).values(id=pid, slug=slug, display_label=label, kind="vendor")
        )
    conn.execute(
        insert(obligation).values(
            id=uuid7(NOW),
            payee_id=pid,
            kind=kind,
            period_label=period,
            amount_due=M("800.00"),
            minimum_due=M("25.00"),
            threshold_due=M(threshold),
            paid=M("0.00"),
            remaining=M("800.00"),
            threshold_remaining=M(threshold),
            due_date=due,
            status=status,
            primary_source="sample-bills/synthetic.pdf",
            before_floor=before_floor,
        )
    )


def test_floor_unset_refuses_even_without_obligations(
    rolled_back_context: Callable[[Role], FinanceContext],
) -> None:
    with pytest.raises(
        OperationError, match="^set finance.reminders.min_due_date first$"
    ):
        desired(context(rolled_back_context, settings=""))


def test_read_uses_owner_connection_and_refuses_member(
    rolled_back_context: Callable[[Role], FinanceContext],
    owner_conn: Connection,
) -> None:
    add(owner_conn)
    catalogue.load_all()
    spec = operations.SPECS["finance.reminder.desired"]
    assert spec.safety_class is SafetyClass.READ
    assert spec.connection == "owner"
    assert spec.tool_name is None
    assert spec.roles == {Role.OWNER, Role.SERVICE}
    for role in (Role.OWNER, Role.SERVICE):
        assert len(desired(context(rolled_back_context, role=role)).tickets) == 1
    with pytest.raises(RoleRefused):
        desired(context(rolled_back_context, role=Role.MEMBER))


@pytest.mark.parametrize(
    "due,status,before_floor",
    [
        (None, "open", False),
        (D(2025, 12, 31), "open", False),
        (D(2026, 5, 20), "open", True),
        (D(2026, 5, 20), "presumed_paid", False),
        (D(2026, 5, 20), "disputed", False),
    ],
    ids=[
        "undated",
        "before-date-floor",
        "stored-floor-marker",
        "presumed-paid",
        "disputed",
    ],
)
def test_ineligible_obligations_emit_nothing(
    rolled_back_context: Callable[[Role], FinanceContext],
    owner_conn: Connection,
    due: dt.date | None,
    status: str,
    before_floor: bool,
) -> None:
    add(owner_conn, due=due, status=status, before_floor=before_floor)
    assert desired(context(rolled_back_context)).tickets == []


@pytest.mark.parametrize(
    "status,threshold", [("partial", "0.00"), ("paid", "0.00"), ("superseded", "40.00")]
)
def test_settled_eligible_periods_return_done_for_reconciliation(
    rolled_back_context: Callable[[Role], FinanceContext],
    owner_conn: Connection,
    status: str,
    threshold: str,
) -> None:
    add(owner_conn, status=status, threshold=threshold)
    (ticket,) = desired(context(rolled_back_context)).tickets
    assert ticket.status == "done"
    assert ticket.severity == "normal"


def test_floor_is_inclusive_and_contract_contains_only_policy_content(
    rolled_back_context: Callable[[Role], FinanceContext],
    owner_conn: Connection,
) -> None:
    add(owner_conn, due=D(2026, 1, 1), status="partial", threshold="12.50")
    (ticket,) = desired(context(rolled_back_context)).tickets
    assert ticket.model_dump() == {
        "payee_label": "Sample Utility",
        "amount": M("12.50"),
        "due_date": D(2026, 1, 1),
        "period": "bill-2026-05",
        "source_ref": "finance:sample-utility:bill-2026-05",
        "title": "Sample Utility: 12.50 CAD due 2026-01-01",
        "body": "bill-2026-05: 12.50 CAD",
        "status": "open",
        "wake": None,
        "ministry": "plenty",
        "ticket_type": "finance-bill",
        "severity": "high",
        "provenance": "status-poll",
    }


@pytest.mark.parametrize(
    "zone,due,when,wake",
    [
        (
            "America/Vancouver",
            D(2026, 3, 12),
            "2026-03-01T12:00:00+00:00",
            "2026-03-07T16:00:00+00:00",
        ),
        (
            "America/Vancouver",
            D(2026, 3, 13),
            "2026-03-01T12:00:00+00:00",
            "2026-03-08T15:00:00+00:00",
        ),
        (
            "America/Creston",
            D(2026, 5, 20),
            "2026-05-15T15:00:00+00:00",
            "2026-05-15T15:00:00+00:00",
        ),
        ("America/Creston", D(2026, 5, 20), "2026-05-15T15:00:01+00:00", None),
    ],
    ids=["winter-offset", "spring-offset", "wake-now", "wake-passed"],
)
def test_local_wake_converts_to_utc_and_passed_wake_is_absent(
    rolled_back_context: Callable[[Role], FinanceContext],
    owner_conn: Connection,
    zone: str,
    due: dt.date,
    when: str,
    wake: str | None,
) -> None:
    add(owner_conn, due=due)
    settings = FLOOR + f'[finance.locale]\ntimezone = "{zone}"\n'
    (ticket,) = desired(
        context(
            rolled_back_context, settings=settings, when=dt.datetime.fromisoformat(when)
        )
    ).tickets
    assert ticket.wake == (dt.datetime.fromisoformat(wake) if wake else None)
    assert ticket.severity == "normal"


def test_wake_settings_and_overdue_use_local_date(
    rolled_back_context: Callable[[Role], FinanceContext],
    owner_conn: Connection,
) -> None:
    add(owner_conn, due=D(2026, 5, 20))
    settings = (
        FLOOR + 'wake_days_before = 2\nwake_time = "09:30"\n'
        '[finance.locale]\ntimezone = "America/Vancouver"\n'
    )
    (ticket,) = desired(context(rolled_back_context, settings=settings)).tickets
    assert ticket.wake == dt.datetime(2026, 5, 18, 16, 30, tzinfo=dt.UTC)
    for when, expected in [
        ("2026-05-21T06:59:59+00:00", "normal"),
        ("2026-05-21T07:00:00+00:00", "high"),
    ]:
        (ticket,) = desired(
            context(
                rolled_back_context,
                settings=settings,
                when=dt.datetime.fromisoformat(when),
            )
        ).tickets
        assert ticket.severity == expected


def test_card_minimum_stays_on_its_statement_when_next_statement_loads(
    own: Ledger,
) -> None:
    minimum = paid_card(D(2025, 3, 25), "25.00")
    with_settings(own, '[finance.reminders]\nmin_due_date = "2025-01-01"\n')
    own.ctx = dataclasses.replace(
        own.ctx, clock=FakeClock(dt.datetime(2025, 3, 30, 12, tzinfo=dt.UTC))
    )
    statement_n(own)
    card_export(own, minimum)
    own.derive()
    first = {t.source_ref.rsplit(":", 1)[1]: t for t in desired(own.ctx).tickets}
    assert first[N].status == "done"
    assert ": 0.00 CAD due " in first[N].title
    statement_n1(own, minimum)
    own.derive()
    after = {t.source_ref.rsplit(":", 1)[1]: t for t in desired(own.ctx).tickets}
    assert after[N].status == "done"
    assert after[N1].status == "open"
    assert ": 30.00 CAD due " in after[N1].title
    assert after[N1].wake == dt.datetime(2025, 4, 20, 15, tzinfo=dt.UTC)


def test_builder_accepts_only_the_typed_four_field_policy() -> None:
    policy = PayloadPolicy(
        payee_label="Sample Utility",
        amount=M("12.50"),
        due_date=D(2026, 5, 20),
        period="bill-2026-05",
    )
    payload = build_payload(policy, registered_payees={"Sample Utility"})
    assert payload.title == "Sample Utility: 12.50 CAD due 2026-05-20"
    assert payload.body == "bill-2026-05: 12.50 CAD"
    with pytest.raises(PayloadRefused):
        build_payload(policy.model_dump(), registered_payees={"Sample Utility"})  # type: ignore[arg-type]  # Deliberate untyped boundary probe.


@pytest.mark.parametrize("extra", ["memo", "balance", "account_number", "counterparty"])
def test_typed_policy_refuses_restricted_extra_fields(extra: str) -> None:
    with pytest.raises(ValidationError):
        PayloadPolicy.model_validate(
            {
                "payee_label": "Sample Utility",
                "amount": M("12.50"),
                "due_date": D(2026, 5, 20),
                "period": "bill-2026-05",
                extra: "private free text",
            }
        )


@pytest.mark.parametrize(
    "label,period,amount,registered",
    [
        ("Sample 999999", "bill-2026-05", "12.50", True),
        ("Sample 9999 9999 9999 9991", "bill-2026-05", "12.50", True),
        ("Sample 9999-99**-****-9991", "bill-2026-05", "12.50", True),
        ("Sample Utility", "999999", "12.50", True),
        ("Sample Utility", "memo with private text", "12.50", True),
        ("Sample Utility", "bill:2026-05", "12.50", True),
        ("Sample Utility", "bill-2026-05", "999999.00", True),
        ("Sample Counterparty", "bill-2026-05", "12.50", False),
    ],
    ids=[
        "digit-run",
        "grouped-card",
        "masked-card",
        "period-digits",
        "memo-period",
        "colon-period",
        "amount-digits",
        "unregistered-counterparty",
    ],
)
def test_builder_rejects_restricted_shapes_even_when_label_is_registered(
    label: str,
    period: str,
    amount: str,
    registered: bool,
) -> None:
    policy = PayloadPolicy(
        payee_label=label, amount=M(amount), due_date=D(2026, 5, 20), period=period
    )
    with pytest.raises(PayloadRefused):
        build_payload(
            policy, registered_payees={label} if registered else {"Sample Utility"}
        )


@pytest.mark.parametrize("amount", ["-0.01", "NaN", "Infinity", "0.001"])
def test_policy_refuses_invalid_amounts(amount: str) -> None:
    with pytest.raises(ValidationError):
        PayloadPolicy(
            payee_label="Sample Utility",
            amount=M(amount),
            due_date=D(2026, 5, 20),
            period="bill-2026-05",
        )


def test_operation_applies_payload_policy_without_leaking_bad_input(
    rolled_back_context: Callable[[Role], FinanceContext],
    owner_conn: Connection,
) -> None:
    label = "Sample 999999"
    add(owner_conn, label=label)
    with pytest.raises(PayloadRefused) as caught:
        desired(context(rolled_back_context))
    assert label not in str(caught.value)


@pytest.mark.parametrize("slug", ["999999", "9999-9999-9999-9991"])
def test_operation_refuses_unsafe_source_keys(
    rolled_back_context: Callable[[Role], FinanceContext],
    owner_conn: Connection,
    slug: str,
) -> None:
    add(owner_conn, slug=slug)
    with pytest.raises(PayloadRefused):
        desired(context(rolled_back_context))


def test_builder_revalidates_a_constructed_policy_without_echoing_input() -> None:
    policy = PayloadPolicy.model_construct(
        payee_label="Sample Utility",
        amount=M("-0.01"),
        due_date=D(2026, 5, 20),
        period="bill-2026-05",
    )
    with pytest.raises(PayloadRefused, match="^invalid reminder payload policy$"):
        build_payload(policy, registered_payees={"Sample Utility"})


def test_policy_refuses_float_money() -> None:
    with pytest.raises(ValidationError):
        PayloadPolicy.model_validate(
            {
                "payee_label": "Sample Utility",
                "amount": 12.5,
                "due_date": D(2026, 5, 20),
                "period": "bill-2026-05",
            }
        )
