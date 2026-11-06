"""Job declarations. A callback owns its work, never the scheduler transaction.

Operation-based callbacks dispatch as SERVICE. Reminder reconciliation is a root
with its own durable transactions, called only after the derive dispatch commits.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from rheo_finance.context import FinanceContext
from rheo_finance.contracts import Role
from rheo_finance.derive.ops import DeriveOut
from rheo_finance.operations import dispatch, owner_transaction
from rheo_finance.rates.valet import FetchOut
from rheo_finance.sinks import current
from rheo_finance.sinks.gate import is_enabled

Detail = Literal[
    "completed",
    "skipped: sink off",
    "pending: current not configured",
    "pending",
    "failed",
]


@dataclass(frozen=True)
class JobResult:
    # None is pending/skipped: neither success nor outbound failure.
    ok: bool | None
    detail_code: Detail = "completed"
    input_digest: str | None = None


@dataclass(frozen=True)
class JobSpec:
    name: str
    callback: Callable[[FinanceContext], JobResult]


JOBS: dict[str, JobSpec] = {}


def register_job(spec: JobSpec) -> None:
    if re.fullmatch(r"finance\.[a-z_]+", spec.name) is None:
        raise ValueError("job name must be finance.<name>")
    if spec.name in JOBS:
        raise ValueError("job already registered")
    JOBS[spec.name] = spec


def _file(ctx: FinanceContext) -> JobResult:
    dispatch("finance.document.file", {}, Role.SERVICE, ctx=ctx)
    return JobResult(ok=True)


def _derive(ctx: FinanceContext) -> JobResult:
    out = dispatch("finance.ledger.derive", {}, Role.SERVICE, ctx=ctx)
    assert isinstance(out, DeriveOut)
    return JobResult(ok=True, input_digest=out.fingerprint)


def _reminders(ctx: FinanceContext) -> JobResult:
    # Disabled delivery does not require the owner's reminder floor or credentials.
    with owner_transaction(ctx) as uow:
        if not is_enabled(uow, "current"):
            return JobResult(ok=None, detail_code="skipped: sink off")
    # The root retains its own gate and per-request checks; no outer transaction.
    out = current.sync_reminders(ctx)
    if out.state == "synced":
        return JobResult(ok=True)
    if out.state == "failed":
        return JobResult(ok=False, detail_code="failed")
    if out.reason == "sink_off":
        return JobResult(ok=None, detail_code="skipped: sink off")
    if out.reason == "not_configured":
        return JobResult(ok=None, detail_code="pending: current not configured")
    return JobResult(ok=None, detail_code="pending")


def _rates(ctx: FinanceContext) -> JobResult:
    out = dispatch("finance.rate.fetch", {}, Role.SERVICE, ctx=ctx)
    assert isinstance(out, FetchOut)
    if not out.enabled:
        return JobResult(ok=None, detail_code="skipped: sink off")
    if out.failed or out.empty:
        return JobResult(ok=False, detail_code="failed")
    return JobResult(ok=True)


for _name, _callback in (
    ("finance.file_downloads", _file),
    ("finance.derive", _derive),
    ("finance.reminders_sync", _reminders),
    ("finance.rates", _rates),
):
    register_job(JobSpec(name=_name, callback=_callback))
