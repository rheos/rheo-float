"""``finance sink approve <sink>``: the one path that records a sink approval (FR-65).

It refuses unless stdin and stdout are both terminals, prints the preview, its digest
and whether the setting is true, reads one typed line that must equal
``approve <sink> <first 8 hex of the preview digest>``, and only then dispatches
``finance.sink.approve`` as the owner, whose handler recomputes the preview and
refuses when the items changed. It never sets the setting.

Only ``cli.main`` imports this module (an import-graph test holds that), so no job,
sink, model step or other module can reach it. The terminal check stops a plain tool
call with no terminal and nothing more; see the repo CLAUDE.md on the real boundary.
``isatty`` and ``read_line`` are injectable so tests can drive it with fake terminals.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Callable

from rheo_finance.render import Report, emit, emit_error

STDIN_FD = 0
STDOUT_FD = 1


def _read_stdin_line() -> str:
    return sys.stdin.readline()


def run(
    sink: str,
    *,
    isatty: Callable[[int], bool] = os.isatty,
    read_line: Callable[[], str] = _read_stdin_line,
) -> int:
    label = f"finance sink approve {sink}"
    if not (isatty(STDIN_FD) and isatty(STDOUT_FD)):
        emit_error(
            Report(
                [
                    f"{label}: refused. It runs only in a terminal: stdin and stdout"
                    " must both be terminals.",
                    "Nothing was recorded.",
                ]
            )
        )
        return 1

    from rheo_finance import composition, operations
    from rheo_finance.contracts import Role
    from rheo_finance.sinks.ops import Preview, Recorded

    ctx = composition.build_context(Role.OWNER)
    try:
        preview = operations.dispatch(
            "finance.sink.preview", {"sink": sink}, Role.OWNER, ctx=ctx
        )
    except operations.OperationError as exc:
        emit_error(Report([f"{label}: refused. {exc}"]))
        return 1
    assert isinstance(preview, Preview)
    code = preview.digest[:8]
    expected = f"approve {sink} {code}"
    emit(preview.text)
    emit(f"To approve, type exactly: {expected}")
    typed = read_line().strip()
    if typed != expected:
        emit_error(
            Report(
                [
                    f"{label}: refused. The typed line did not match.",
                    "Nothing was recorded.",
                ]
            )
        )
        return 1
    try:
        out = operations.dispatch(
            "finance.sink.approve",
            {"sink": sink, "digest_prefix": code},
            Role.OWNER,
            ctx=ctx,
        )
    except operations.OperationError as exc:
        emit_error(Report([f"{label}: refused. {exc}", "Nothing was recorded."]))
        return 1
    assert isinstance(out, Recorded)
    if out.setting_enabled:
        setting = f"Setting finance.sinks.{sink}.enabled is true: the sink is on."
    else:
        setting = (
            f"Setting finance.sinks.{sink}.enabled is false: set it to true in"
            " settings.toml to turn the sink on."
        )
    emit(
        Report(
            [
                f"{label}: approved against preview digest {code}.",
                f"Recorded as decision {out.decision_id}.",
                setting,
            ]
        )
    )
    return 0


def cmd_approve(args: argparse.Namespace) -> int:
    return run(args.sink)
