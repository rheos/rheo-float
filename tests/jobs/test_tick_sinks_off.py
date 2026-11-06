"""A real tick with pending synthetic reminders and rates never opens an off sink."""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Literal

import pytest
from sqlalchemy import Connection, select

from conftest import StubServer
from derive.support import Ledger, fx_lines
from jobs.test_tick import configured
from reminders.test_desired import add
from rheo_finance import catalogue
from rheo_finance.cli import jobs as jobs_cli
from rheo_finance.cli.main import main
from rheo_finance.context import FinanceContext
from rheo_finance.contracts import Role
from rheo_finance.http import Client
from rheo_finance.jobs import registry as jobs_registry
from rheo_finance.jobs.registry import JOBS, JobResult, JobSpec
from rheo_finance.jobs.tick import tick
from rheo_finance.operations import dispatch
from rheo_finance.rates import valet
from rheo_finance.settings import Settings
from rheo_finance.sinks import current
from rheo_finance.sinks.gate import SinkDisabled
from rheo_finance.storage.schema import job_run, sink_ticket
from sinks.support import CurrentStub


def test_public_tick_all_sinks_off_with_pending_intents_is_not_failure(
    rolled_back_context: Callable[[Role], FinanceContext],
    tmp_path: Path,
    owner_conn: Connection,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    current_settings: Settings,
    fx_ledger: Ledger,
    current_stub: CurrentStub,
    stub_server: StubServer,
    sink_settings: Callable[..., None],
) -> None:
    catalogue.load_all()
    sink_settings()
    monkeypatch.setattr(current, "load_settings", lambda: current_settings)
    fx_ledger.file(
        "synthetic-fx/2025-12-10.txt",
        fx_lines("USD", "2025-12-10 -10.00 - - SAMPLE SHOP 99951"),
    )
    fx_ledger.derive()
    ctx = configured(replace(fx_ledger.ctx, role=Role.SERVICE), tmp_path)
    assert valet.plan(fx_ledger.uow)
    add(owner_conn)
    # The derive job already completed for these inputs. This leaves the pending
    # reminders and stored lines in place for the public tick's real callbacks.
    from rheo_finance.context import UnitOfWork
    from rheo_finance.derive.fingerprint import fingerprint

    at = ctx.clock.now_utc()
    digest = fingerprint(UnitOfWork(owner_conn), ctx).digest
    dispatch(
        "finance.job.record",
        dict(
            job="finance.derive",
            started_at=at,
            finished_at=at,
            ok=True,
            detail_code="completed",
            input_digest=digest,
        ),
        Role.SERVICE,
        ctx=ctx,
    )
    monkeypatch.setattr(valet, "ENDPOINT", valet.Endpoint(stub_server.host, "http"))
    monkeypatch.setattr(
        valet,
        "make_client",
        lambda u, s: Client(
            "rates", s, valet.ALLOWLIST, uow=u, allowed_schemes=("http",)
        ),
    )
    monkeypatch.setattr(jobs_cli, "_context", lambda: ctx)
    code = main(["jobs", "tick"])
    assert current_stub.requests == [] and stub_server.requests == []
    assert code == 0
    assert owner_conn.execute(select(sink_ticket)).all() == []
    runs = {row.job: row for row in owner_conn.execute(select(job_run)).all()}
    for name in ("finance.rates", "finance.reminders_sync"):
        assert runs[name].ok is None
        assert runs[name].detail_code == "skipped: sink off"
    output = capsys.readouterr().out
    assert "0 failed" in output and "not synced" not in output
    status = dispatch("finance.status.get", {}, Role.SERVICE, ctx=ctx)
    assert "current" in next(
        line["text"] for line in status.model_dump()["lines"] if line["line_no"] == 7
    )
    assert tick(ctx).runs == ()


@pytest.mark.parametrize(
    "state,reason,ok,detail",
    [
        ("synced", None, True, "completed"),
        ("pending", "sink_off", None, "skipped: sink off"),
        ("pending", "not_configured", None, "pending: current not configured"),
        ("pending", "configuration_error", None, "pending"),
        ("failed", "request_failed", False, "failed"),
    ],
)
def test_current_result_controls_success_anchor_and_bounded_retry(
    state: Literal["synced", "pending", "failed"],
    reason: str | None,
    ok: bool | None,
    detail: str,
    rolled_back_context: Callable[[Role], FinanceContext],
    tmp_path: Path,
    owner_conn: Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from rheo_finance.clock import FakeClock
    from rheo_finance.sinks.current import SyncResult

    ctx = configured(rolled_back_context(Role.SERVICE), tmp_path)
    # These cases exercise the enabled callback result contract explicitly.
    monkeypatch.setattr(jobs_registry, "is_enabled", lambda u, sink: True)
    monkeypatch.setattr(current, "sync_reminders", lambda c: SyncResult(state, reason))
    first = tick(ctx)
    result = next(r.result for r in first.runs if r.job == "finance.reminders_sync")
    assert (result.ok, result.detail_code) == (ok, detail)
    row = owner_conn.execute(
        select(job_run).where(job_run.c.job == "finance.reminders_sync")
    ).one()
    assert row.ok is ok and row.detail_code == detail
    again = tick(ctx)
    assert ("finance.reminders_sync" in [r.job for r in again.runs]) is (ok is False)
    clock = ctx.clock
    assert isinstance(clock, FakeClock)
    clock.advance(dt.timedelta(minutes=15))
    assert "finance.reminders_sync" in [r.job for r in tick(ctx).runs]


def test_sink_disabled_exception_is_pending_without_success_or_failure(
    rolled_back_context: Callable[[Role], FinanceContext],
    tmp_path: Path,
    owner_conn: Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = configured(rolled_back_context(Role.SERVICE), tmp_path)

    def off(_ctx: FinanceContext) -> JobResult:
        raise SinkDisabled("synthetic")

    monkeypatch.setitem(JOBS, "finance.rates", JobSpec("finance.rates", off))
    result = tick(ctx)
    out = next(each.result for each in result.runs if each.job == "finance.rates")
    assert out == JobResult(ok=None, detail_code="skipped: sink off")
    assert owner_conn.execute(select(sink_ticket)).all() == []
    assert tick(ctx).runs == ()


def test_public_fresh_install_off_sinks_and_unset_floor_do_not_fail(
    rolled_back_context: Callable[[Role], FinanceContext],
    tmp_path: Path,
    owner_conn: Connection,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    current_stub: CurrentStub,
    stub_server: StubServer,
    sink_settings: Callable[..., None],
) -> None:
    from rheo_finance.settings import parse_settings

    sink_settings()
    archive = tmp_path / "archive"
    archive.mkdir()
    ctx = replace(
        rolled_back_context(Role.SERVICE),
        settings=parse_settings(
            f'[finance.paths]\narchive = "{archive}"\n'
            f'downloads = "{tmp_path / "downloads"}"\n'
        ),
    )
    assert ctx.settings.get_date("finance.reminders.min_due_date") is None
    monkeypatch.setattr(valet, "ENDPOINT", valet.Endpoint(stub_server.host, "http"))
    monkeypatch.setattr(jobs_cli, "_context", lambda: ctx)
    assert main(["jobs", "tick"]) == 0
    rows = owner_conn.execute(select(job_run)).all()
    assert len(rows) == 4 and not any(row.ok is False for row in rows)
    reminder = next(row for row in rows if row.job == "finance.reminders_sync")
    assert reminder.ok is None and reminder.detail_code == "skipped: sink off"
    assert owner_conn.execute(select(sink_ticket)).all() == []
    assert current_stub.requests == [] and stub_server.requests == []
    assert "0 failed" in capsys.readouterr().out


def test_enabled_current_still_refuses_unset_floor_before_requests(
    current_context: FinanceContext,
    current_stub: CurrentStub,
    owner_conn: Connection,
    tmp_path: Path,
) -> None:
    from rheo_finance.operations import OperationError

    ctx = configured(current_context, tmp_path)
    ctx = replace(
        ctx,
        settings=replace(
            ctx.settings,
            values={**ctx.settings.values, "finance.reminders.min_due_date": None},
        ),
    )
    with pytest.raises(
        OperationError, match="set finance.reminders.min_due_date first"
    ):
        current.sync_reminders(ctx)
    result = tick(ctx)
    reminder = next(r.result for r in result.runs if r.job == "finance.reminders_sync")
    assert reminder.ok is False and reminder.detail_code == "failed"
    assert current_stub.requests == []
    assert owner_conn.execute(select(sink_ticket)).all() == []
