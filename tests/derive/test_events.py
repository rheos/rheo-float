"""Events and state notices through derive.run, using only synthetic layouts."""

from __future__ import annotations

import dataclasses
import datetime as dt
import uuid
from decimal import Decimal

import pytest
from sqlalchemy import func, select, update

from rheo_finance.clock import FakeClock
from rheo_finance.derive.fingerprint import fingerprint
from rheo_finance.derive.run import run
from rheo_finance.derive.stage16 import (
    OBLIGATION_CREATED,
    OBLIGATION_SUPERSEDED,
    PAYMENT_REVERSED,
    STATEMENT_AVAILABLE,
    STATEMENT_OVERDUE,
    StatementReady,
    statement_ready_text,
)
from rheo_finance.manual.stage2 import account_id
from rheo_finance.storage.schema import (
    document_line,
    event,
    notice,
    obligation,
    overdrawn_spell,
    parsed_overdrawn,
)

from .support import CSV_IDENTIFIER, FIXTURES, D, Ledger, read_as_text
from .test_payment_application import bills, with_settings
from .test_presumed_paid import presume, rows
from .test_reversals import export

STATEMENT = (FIXTURES / "vancity_statement_split.txt").read_text(encoding="utf-8")


def clock(ledger: Ledger, day: str) -> FakeClock:
    fake = FakeClock(dt.datetime.fromisoformat(day).replace(tzinfo=dt.UTC))
    ledger.ctx = dataclasses.replace(ledger.ctx, clock=fake)
    return fake


def statement(ledger: Ledger) -> None:
    ledger.chequing()
    ledger.file("vancity/2026-01-10.pdf", STATEMENT)
    with_settings(ledger, '[finance.locale]\ntimezone = "America/Creston"\n')


def ids(ledger: Ledger, kind: str) -> set[uuid.UUID]:
    return set(ledger.conn.scalars(select(event.c.id).where(event.c.type == kind)))


def test_grace_boundary_uses_local_day_without_data_change(ledger: Ledger) -> None:
    statement(ledger)
    fake = clock(ledger, "2026-02-17T06:59:00")  # local 16 February
    ledger.derive()
    assert not ids(ledger, STATEMENT_OVERDUE)
    before = fingerprint(ledger.uow, ledger.ctx)
    fake.advance(dt.timedelta(minutes=1))
    after = fingerprint(ledger.uow, ledger.ctx)
    assert {key for key in before.parts if before.parts[key] != after.parts[key]} == {
        "local_date"
    }
    ledger.derive()
    first = ids(ledger, STATEMENT_OVERDUE)
    assert len(first) == 1
    first_notices = set(ledger.conn.scalars(select(notice.c.key)))
    ledger.derive()
    assert ids(ledger, STATEMENT_OVERDUE) == first
    assert set(ledger.conn.scalars(select(notice.c.key))) == first_notices
    assert ledger.conn.scalar(
        select(event.c.occurred_on).where(event.c.type == STATEMENT_OVERDUE)
    ) == D(2026, 2, 17)


def test_rebuild_restores_event_ids_and_retains_notice_delivery_state(
    ledger: Ledger,
) -> None:
    statement(ledger)
    fake = clock(ledger, "2026-02-17T12:00:00")
    ledger.derive()
    expected = set(ledger.conn.scalars(select(event.c.id)))
    first_seen = fake.now_utc()
    # A delivery outcome must survive just as first_seen does, even on a reparse.
    ledger.conn.execute(update(notice).values(state="sent", sent_at=first_seen))
    fake.advance(dt.timedelta(days=2))
    run(ledger.uow, ledger.ctx, extract_pdf=read_as_text, reparse=True)
    assert set(ledger.conn.scalars(select(event.c.id))) == expected
    found = ledger.conn.execute(select(notice)).all()
    assert len(found) == 2
    assert all(
        row.first_seen_at == first_seen
        and row.sent_at == first_seen
        and row.state == "sent"
        for row in found
    )


def test_availability_fallback_uses_global_default_lag(ledger: Ledger) -> None:
    statement(ledger)
    fake = clock(ledger, "2026-02-12T06:59:00")
    ledger.derive()
    assert not ids(ledger, STATEMENT_AVAILABLE)
    fake.advance(dt.timedelta(minutes=1))
    ledger.derive()
    assert len(ids(ledger, STATEMENT_AVAILABLE)) == 1
    assert not ids(ledger, STATEMENT_OVERDUE)
    assert ledger.conn.scalar(
        select(event.c.occurred_on).where(event.c.type == STATEMENT_AVAILABLE)
    ) == D(2026, 2, 12)
    assert (
        statement_ready_text("Sample daily")
        == "Sample daily statement is ready to download"
    )


def test_ready_notice_is_immediate_and_deduplicates_fallback(ledger: Ledger) -> None:
    statement(ledger)
    fake = clock(ledger, "2026-02-10T12:00:00")
    fact = StatementReady(account_id("chequing"), D(2026, 2, 10), D(2026, 2, 10))

    def ready(_uow: object) -> tuple[StatementReady, ...]:
        return (fact, fact)

    run(ledger.uow, ledger.ctx, extract_pdf=read_as_text, statement_ready=ready)
    original = ids(ledger, STATEMENT_AVAILABLE)
    assert len(original) == 1
    first_seen = ledger.conn.scalar(select(notice.c.first_seen_at))
    fake.advance(dt.timedelta(days=3))
    run(ledger.uow, ledger.ctx, extract_pdf=read_as_text, statement_ready=ready)
    assert ids(ledger, STATEMENT_AVAILABLE) == original
    # A rebuild without the mail input still has the same fallback identity.
    run(ledger.uow, ledger.ctx, extract_pdf=read_as_text, reparse=True)
    assert ids(ledger, STATEMENT_AVAILABLE) == original
    assert ledger.conn.scalar(select(func.count()).select_from(notice)) == 1
    assert ledger.conn.scalar(select(notice.c.first_seen_at)) == first_seen


def test_ready_without_history_but_not_for_unregistered_or_filed_period(
    ledger: Ledger,
) -> None:
    statement(ledger)
    clock(ledger, "2026-01-15T12:00:00")

    def ready(_uow: object) -> tuple[StatementReady, ...]:
        return (
            StatementReady(account_id("chequing"), D(2026, 1, 10), D(2026, 1, 11)),
            StatementReady(account_id("absent"), D(2026, 1, 12), D(2026, 1, 13)),
            StatementReady(account_id("chequing"), D(2026, 1, 20), D(2026, 1, 21)),
        )

    run(ledger.uow, ledger.ctx, extract_pdf=read_as_text, statement_ready=ready)
    assert not ids(ledger, STATEMENT_AVAILABLE)
    (ledger.archive / "vancity/2026-01-10.pdf").unlink()

    def no_history(_uow: object) -> tuple[StatementReady, ...]:
        return (StatementReady(account_id("chequing"), D(2026, 1, 10), D(2026, 1, 11)),)

    run(ledger.uow, ledger.ctx, extract_pdf=read_as_text, statement_ready=no_history)
    assert len(ids(ledger, STATEMENT_AVAILABLE)) == 1


def test_new_period_document_stops_missing_statement_events(ledger: Ledger) -> None:
    statement(ledger)
    clock(ledger, "2026-02-17T12:00:00")
    ledger.derive()
    assert len(ids(ledger, STATEMENT_OVERDUE)) == 1
    new = (
        STATEMENT.replace("11 DEC 2025", "11 JAN 2026")
        .replace("10 JAN 2026", "10 FEB 2026")
        .replace("15 DEC", "15 JAN")
        .replace("20 DEC", "20 JAN")
    )
    ledger.file("vancity/2026-02-10.pdf", new)
    ledger.derive()
    assert not ids(ledger, STATEMENT_OVERDUE)
    assert not ids(ledger, STATEMENT_AVAILABLE)


@pytest.mark.parametrize(
    "cadence,expected",
    [
        ("monthly", D(2026, 2, 12)),
        ("quarterly", D(2026, 4, 12)),
        ("yearly", D(2027, 1, 12)),
    ],
)
def test_calendar_cadences_and_configured_lag(
    ledger: Ledger,
    cadence: str,
    expected: dt.date,
) -> None:
    statement(ledger)
    path = ledger.data / "manual" / "accounts.csv"
    path.write_text(path.read_text().replace(",monthly,", f",{cadence},"))
    with_settings(ledger, "[finance.statements]\navailability_lag_days = 1\n")
    clock(ledger, f"{expected - dt.timedelta(days=1)}T12:00:00")
    ledger.derive()
    assert len(ids(ledger, STATEMENT_AVAILABLE)) == 1


def test_obligations_created_and_overdue_notices(ledger: Ledger) -> None:
    ledger.chequing()
    ledger.manual("payees.csv", "net,Sample network,vendor,SAMPLE NET,,")
    bills(
        ledger,
        ("net", "20.00", D(2026, 1, 1), D(2026, 1, 5)),
        ("net", "30.00", D(2026, 2, 1), D(2026, 2, 5)),
    )
    clock(ledger, "2026-02-20T12:00:00")
    ledger.derive()
    assert len(ids(ledger, OBLIGATION_CREATED)) == 2
    assert not ids(ledger, OBLIGATION_SUPERSEDED)
    overdue = ledger.conn.execute(
        select(notice).where(notice.c.kind == "overdue")
    ).all()
    active = ledger.conn.execute(
        select(obligation).where(obligation.c.status == "open")
    ).all()
    assert {row.key for row in overdue} == {f"overdue:{row.id}" for row in active}
    assert {row.event_date for row in overdue} == {D(2026, 1, 5), D(2026, 2, 5)}
    assert all(row.state == "pending" for row in overdue)
    assert not set(ledger.conn.scalars(select(notice.c.kind))) - {"overdue"}


@pytest.mark.parametrize("excluded", ["presumed_paid", "before_floor"])
def test_presumed_paid_and_before_floor_never_make_overdue_notices(
    ledger: Ledger,
    excluded: str,
) -> None:
    ledger.chequing()
    ledger.manual("payees.csv", "net,Sample network,vendor,SAMPLE NET,,")
    bills(ledger, ("net", "20.00", D(2026, 1, 1), D(2026, 1, 5)))
    # Keep the first derive before the due date, so no prior notice exists.
    fake = clock(ledger, "2026-01-02T12:00:00")
    ledger.derive()
    if excluded == "presumed_paid":
        presume(ledger, "net bill-2026-01")
    else:
        with_settings(ledger, "[finance.reminders]\nmin_due_date = 2026-01-06\n")
    fake.advance(dt.timedelta(days=20))
    ledger.derive()
    row = rows(ledger)["net bill-2026-01"]
    assert row.remaining == Decimal("20.00")
    assert (
        row.status == "presumed_paid"
        if excluded == "presumed_paid"
        else row.before_floor
    )
    assert (
        ledger.conn.scalar(
            select(func.count()).select_from(notice).where(notice.c.kind == "overdue")
        )
        == 0
    )


def test_payment_reversal_has_actual_reversal_date_and_safe_refs(
    ledger: Ledger,
) -> None:
    ledger.chequing()
    ledger.manual("payees.csv", "net,Sample network,vendor,SAMPLE NET,,")
    ledger.file(
        "vancity/payment.csv",
        export(
            (
                CSV_IDENTIFIER,
                D(2026, 1, 4),
                "INTERAC E-TRANSFER To: SAMPLE NET",
                Decimal("-20.00"),
            ),
            (
                CSV_IDENTIFIER,
                D(2026, 1, 6),
                "E-TRANSFER DEBIT REVERSAL CUSTOMER CANCEL",
                Decimal("20.00"),
            ),
        ),
    )
    ledger.derive()
    found = ledger.conn.execute(
        select(event).where(event.c.type == PAYMENT_REVERSED)
    ).one()
    assert found.occurred_on == D(2026, 1, 6)
    assert set(found.data) == {"payee_ref", "reversal_ref"}


def test_actual_parser_spells_survive_cached_derive_and_rebuild(ledger: Ledger) -> None:
    statement(ledger)
    # The terminal printed balance becomes negative on the second row.
    actual = (
        STATEMENT.replace("30.00", "200.00")
        .replace("32.00", "202.00")
        .replace("118.00", "-52.00")
    )
    ledger.file("vancity/2026-01-10.pdf", actual)
    ledger.derive()
    expected = [(account_id("chequing"), "CAD", D(2025, 12, 20), None)]
    assert [
        tuple(row) for row in ledger.conn.execute(select(overdrawn_spell))
    ] == expected
    assert ledger.conn.scalar(select(func.count()).select_from(parsed_overdrawn)) == 1
    ledger.derive()
    assert [
        tuple(row) for row in ledger.conn.execute(select(overdrawn_spell))
    ] == expected
    run(ledger.uow, ledger.ctx, extract_pdf=read_as_text, reparse=True)
    assert [
        tuple(row) for row in ledger.conn.execute(select(overdrawn_spell))
    ] == expected
    (ledger.archive / "vancity/2026-01-10.pdf").unlink()
    ledger.derive()
    assert ledger.conn.scalar(select(func.count()).select_from(overdrawn_spell)) == 0


def test_split_intermediate_negative_balance_is_not_an_overdrawn_spell(
    ledger: Ledger,
) -> None:
    statement(ledger)
    split = (
        STATEMENT.replace("100.00", "  1.00")
        .replace("148.00", " 49.00")
        .replace("118.00", " 19.00")
    )
    ledger.file("vancity/2026-01-10.pdf", split)
    ledger.derive()
    assert (
        ledger.conn.scalar(
            select(func.count())
            .select_from(document_line)
            .where(document_line.c.running_balance < 0)
        )
        == 1
    )
    assert ledger.conn.scalar(select(func.count()).select_from(overdrawn_spell)) == 0


def test_notice_output_does_not_dirty_the_input_fingerprint(ledger: Ledger) -> None:
    statement(ledger)
    clock(ledger, "2026-02-17T12:00:00")
    before = fingerprint(ledger.uow, ledger.ctx)
    ledger.derive()
    after = fingerprint(ledger.uow, ledger.ctx)
    assert before == after


def test_superseded_obligation_event_names_the_replacement(ledger: Ledger) -> None:
    from .test_obligations import NEWER_VISA_STATEMENT, VISA_STATEMENT, visa

    visa(ledger)
    ledger.file(
        NEWER_VISA_STATEMENT,
        (FIXTURES / "vancity_visa_statement_no_minimum.txt").read_text(),
    )
    ledger.file(VISA_STATEMENT, (FIXTURES / "vancity_visa_statement.txt").read_text())
    ledger.derive()
    found = ledger.conn.execute(
        select(event).where(event.c.type == OBLIGATION_SUPERSEDED)
    ).one()
    older = ledger.conn.execute(
        select(obligation).where(obligation.c.status == "superseded")
    ).one()
    newer = ledger.conn.execute(
        select(obligation).where(obligation.c.supersedes_id == older.id)
    ).one()
    assert found.subject_ref == f"finance.obligation:{older.id}"
    assert found.data == {"replacement_ref": f"finance.obligation:{newer.id}"}


def test_payment_alert_notice_is_written_while_all_sinks_are_disabled(
    ledger: Ledger,
) -> None:
    ledger.chequing()
    ledger.manual("payees.csv", "net,Sample network,vendor,SAMPLE NET,,")
    bills(ledger, ("net", "20.00", D(2026, 1, 1), D(2026, 1, 8)))
    ledger.file(
        "vancity/paid.csv",
        export(
            (
                CSV_IDENTIFIER,
                D(2026, 1, 4),
                "INTERAC E-TRANSFER To: SAMPLE NET",
                Decimal("-20.00"),
            ),
        ),
    )
    ledger.derive()
    detected = ids(ledger, "finance.payment.detected")
    assert len(detected) == 1
    found = ledger.conn.execute(select(notice)).one()
    assert found.key == f"alert:{next(iter(detected))}"
    assert found.kind == "alert" and found.state == "pending"
    assert found.event_date == D(2026, 1, 4)
    assert found.first_seen_at == ledger.ctx.clock.now_utc()
