"""The tick composition root and explicitly requested LaunchAgent installation."""

from __future__ import annotations

import argparse
from typing import TYPE_CHECKING

from rheo_finance.clock import render_local
from rheo_finance.render import Report, emit

if TYPE_CHECKING:
    from rheo_finance.context import FinanceContext


def _context() -> FinanceContext:
    from rheo_finance.composition import build_context
    from rheo_finance.contracts import Role

    return build_context(Role.SERVICE)


def _tick(_args: argparse.Namespace) -> int:
    from rheo_finance.jobs.tick import tick

    ctx = _context()
    result = tick(ctx)
    failed = sum(each.result.ok is False for each in result.runs)
    pending = sum(each.result.ok is None for each in result.runs)
    stamp = render_local(result.as_of, ctx.settings.timezone)
    emit(
        Report(
            [
                f"finance jobs tick: {len(result.runs)} jobs ran, {failed} failed,"
                f" {pending} pending, as of {stamp}"
            ],
            [f"{each.job}: {each.result.detail_code}" for each in result.runs],
        )
    )
    return 1 if failed else 0


def _install_agent(args: argparse.Namespace) -> int:
    from pathlib import Path

    from rheo_finance.db import data_dir
    from rheo_finance.jobs.agent import install_agent

    rendered = install_agent(
        repo=Path(args.repo) if args.repo is not None else None,
        data=data_dir(),
        dry_run=args.dry_run,
    )
    if args.dry_run:
        emit(rendered)
    else:
        emit(Report(["finance jobs install-agent: installed."]))
    return 0


def add_parser(commands: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = commands.add_parser("jobs", help="scheduled job catch-up")
    sub = parser.add_subparsers(dest="jobs_command", required=True)
    sub.add_parser("tick", help="run due jobs once as service").set_defaults(
        handler=_tick
    )
    install = sub.add_parser(
        "install-agent", help="install the primary clone's LaunchAgent"
    )
    install.add_argument("--repo", help="primary clone path")
    install.add_argument(
        "--dry-run", action="store_true", help="render without installing"
    )
    install.set_defaults(handler=_install_agent)
