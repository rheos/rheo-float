"""Public tick scheduling and dirty-check contract, using synthetic local inputs."""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

import pytest
from sqlalchemy import Connection, select

from rheo_finance import catalogue
from rheo_finance.cli import jobs as jobs_cli
from rheo_finance.cli.main import main
from rheo_finance.clock import FakeClock
from rheo_finance.context import FinanceContext
from rheo_finance.contracts import Role
from rheo_finance.derive.fingerprint import fingerprint
from rheo_finance.jobs.registry import JOBS, JobResult, JobSpec
from rheo_finance.jobs.tick import tick
from rheo_finance.operations import owner_transaction
from rheo_finance.settings import parse_settings
from rheo_finance.storage.schema import job_run


def configured(ctx: FinanceContext, tmp_path: Path) -> FinanceContext:
    archive = tmp_path / "archive"
    archive.mkdir(exist_ok=True)
    return replace(
        ctx,
        settings=parse_settings(
            f'[finance.paths]\narchive = "{archive}"\n'
            f'downloads = "{tmp_path / "downloads"}"\n'
            '[finance.reminders]\nmin_due_date = "2026-01-01"\n'
        ),
    )


def test_public_cli_catches_up_once_and_idle_tick_does_nothing(
    rolled_back_context: Callable[[Role], FinanceContext],
    tmp_path: Path,
    owner_conn: Connection,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    ctx = configured(rolled_back_context(Role.SERVICE), tmp_path)
    clock = ctx.clock
    assert isinstance(clock, FakeClock)
    # Replace only callbacks so the public scheduler must choose due work itself.
    calls: list[tuple[str, Role]] = []
    for name in tuple(JOBS):

        def run(c: FinanceContext, name: str = name) -> JobResult:
            calls.append((name, c.role))
            with owner_transaction(c) as uow:
                taken = fingerprint(uow, c).digest
            return JobResult(ok=True, input_digest=taken)

        monkeypatch.setitem(JOBS, name, JobSpec(name=name, callback=run))
    monkeypatch.setattr(jobs_cli, "_context", lambda: ctx)
    assert main(["jobs", "tick"]) == 0
    assert len(calls) == 4 and all(role is Role.SERVICE for _, role in calls)
    calls.clear()
    clock.advance(dt.timedelta(days=3))
    assert main(["jobs", "tick"]) == 0
    assert len(calls) == 4
    assert "as of 2026-01-18 05:00 MST" in capsys.readouterr().out
    calls.clear()
    assert main(["jobs", "tick"]) == 0
    assert calls == []
    assert (
        owner_conn.scalar(select(job_run.c.id).where(job_run.c.ok.is_(False))) is None
    )


def test_real_derive_is_dirty_once_after_midnight(
    rolled_back_context: Callable[[Role], FinanceContext],
    tmp_path: Path,
    owner_conn: Connection,
) -> None:
    catalogue.load_all()
    ctx = configured(rolled_back_context(Role.SERVICE), tmp_path)
    clock = ctx.clock
    assert isinstance(clock, FakeClock)
    clock.set(dt.datetime(2026, 1, 16, 6, 59, tzinfo=dt.UTC))
    assert "finance.derive" in [r.job for r in tick(ctx).runs]
    clock.advance(dt.timedelta(minutes=2))
    assert "finance.derive" in [r.job for r in tick(ctx).runs]
    assert "finance.derive" not in [r.job for r in tick(ctx).runs]
    clock.advance(dt.timedelta(minutes=15))
    assert "finance.derive" not in [r.job for r in tick(ctx).runs]
    assert (
        len(
            owner_conn.execute(
                select(job_run).where(job_run.c.job == "finance.derive")
            ).all()
        )
        == 2
    )


def test_filing_precedes_dirty_check_and_new_inputs_can_derive_in_same_slot(
    rolled_back_context: Callable[[Role], FinanceContext],
    tmp_path: Path,
    owner_conn: Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = configured(rolled_back_context(Role.SERVICE), tmp_path)
    tick(ctx)
    clock = ctx.clock
    assert isinstance(clock, FakeClock)
    clock.advance(dt.timedelta(minutes=15))

    def file_new(_ctx: FinanceContext) -> JobResult:
        (tmp_path / "archive" / "synthetic.txt").write_text("synthetic input")
        return JobResult(ok=True)

    monkeypatch.setitem(
        JOBS, "finance.file_downloads", JobSpec("finance.file_downloads", file_new)
    )
    assert "finance.derive" in [r.job for r in tick(ctx).runs]
    before = owner_conn.execute(
        select(job_run).where(job_run.c.job == "finance.derive")
    ).all()
    assert len(before) == 2
    assert tick(ctx).runs == ()
    manual = tmp_path / "manual"
    manual.mkdir(exist_ok=True)
    (manual / "filed_years.csv").write_text("year,filed_on,note\n")
    assert [r.job for r in tick(ctx).runs] == ["finance.derive"]


def test_configured_schedule_boundaries_and_schema_refusal_are_public(
    rolled_back_context: Callable[[Role], FinanceContext],
    tmp_path: Path,
    owner_conn: Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import importlib

    from rheo_finance.operations import OperationError

    module = importlib.import_module("rheo_finance.jobs.tick")
    ctx = configured(rolled_back_context(Role.SERVICE), tmp_path)
    ctx = replace(
        ctx,
        settings=parse_settings(
            f'[finance.paths]\narchive = "{tmp_path / "archive"}"\n'
            '[finance.reminders]\nmin_due_date = "2026-01-01"\n'
            "[finance.jobs]\ntick_minutes = 60\n"
        ),
    )
    clock = ctx.clock
    assert isinstance(clock, FakeClock)
    tick(ctx)
    clock.advance(dt.timedelta(minutes=59))
    assert tick(ctx).runs == ()
    clock.advance(dt.timedelta(minutes=1))
    assert {r.job for r in tick(ctx).runs} == {
        "finance.file_downloads",
        "finance.reminders_sync",
        "finance.rates",
    }
    previous = owner_conn.execute(select(job_run)).all()
    monkeypatch.setattr(module, "head_revision", lambda: "future")
    with pytest.raises(OperationError, match="schema_behind"):
        tick(ctx)
    assert owner_conn.execute(select(job_run)).all() == previous


def test_failed_derive_prevents_current_sync_and_persists_sanitized_failure(
    rolled_back_context: Callable[[Role], FinanceContext],
    tmp_path: Path,
    owner_conn: Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = configured(rolled_back_context(Role.SERVICE), tmp_path)

    def broken(_ctx: FinanceContext) -> JobResult:
        raise ValueError("synthetic private payload must never be persisted")

    def forbidden(_ctx: FinanceContext) -> JobResult:
        pytest.fail("Current must wait for successful derive")

    monkeypatch.setitem(JOBS, "finance.derive", JobSpec("finance.derive", broken))
    monkeypatch.setitem(
        JOBS, "finance.reminders_sync", JobSpec("finance.reminders_sync", forbidden)
    )
    result = tick(ctx)
    assert "finance.reminders_sync" not in [r.job for r in result.runs]
    row = owner_conn.execute(
        select(job_run).where(job_run.c.job == "finance.derive")
    ).one()
    assert row.ok is False and row.detail_code == "failed"


@pytest.mark.parametrize("role", [Role.OWNER, Role.MEMBER])
def test_tick_refuses_non_service_before_work(
    role: Role,
    rolled_back_context: Callable[[Role], FinanceContext],
    owner_conn: Connection,
) -> None:
    from rheo_finance.operations import RoleRefused

    with pytest.raises(RoleRefused):
        tick(rolled_back_context(role))
    assert owner_conn.execute(select(job_run)).all() == []
