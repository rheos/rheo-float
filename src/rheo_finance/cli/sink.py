"""``finance sink status|preview|disable``, and the parser for ``finance sink approve``.

The approve handler lives in ``cli.approve``, which only ``cli.main`` imports: main
passes it in to ``add_parser``, so this module never names it. Like every module
``cli.main`` registers, this one keeps sqlalchemy and storage out of its top-level
imports.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable

from rheo_finance.render import Report, emit, emit_error
from rheo_finance.settings import SINKS

Handler = Callable[[argparse.Namespace], int]


def _dispatch(label: str, name: str, payload: dict[str, str]) -> object | None:
    """Dispatch as the owner; a refusal is reported and returns None."""
    from rheo_finance import composition, operations
    from rheo_finance.contracts import Role

    try:
        return operations.dispatch(
            name, payload, Role.OWNER, ctx=composition.build_context(Role.OWNER)
        )
    except operations.OperationError as exc:
        emit_error(Report([f"{label}: refused. {exc}"]))
        return None


def _cmd_status(_args: argparse.Namespace) -> int:
    from rheo_finance.clock import render_local
    from rheo_finance.settings import load_settings
    from rheo_finance.sinks.ops import StateOut

    label = "finance sink status"
    out = _dispatch(label, "finance.sink.state", {})
    if out is None:
        return 1
    assert isinstance(out, StateOut)
    tz = load_settings().timezone
    enabled = [each.sink for each in out.sinks if each.enabled]
    stale = [each.sink for each in out.sinks if each.approval == "stale"]
    headline = [
        f"{label}: {len(enabled)} of {len(out.sinks)} sinks enabled"
        + (f" ({', '.join(enabled)})." if enabled else "."),
        "Stale approvals: " + (", ".join(stale) if stale else "none") + ".",
    ]
    detail = []
    for each in out.sinks:
        setting = "true" if each.setting_enabled else "false"
        if each.approved_at is None:
            approval = "no approval"
        else:
            when = render_local(each.approved_at, tz)
            approval = f"approved {when}, {each.approval}"
        word = "on" if each.enabled else "off"
        detail.append(f"{each.sink}: {word}; setting {setting}; {approval}.")
    emit(Report(headline, detail))
    return 0


def _cmd_preview(args: argparse.Namespace) -> int:
    from rheo_finance.sinks.ops import Preview

    label = f"finance sink preview {args.sink}"
    out = _dispatch(label, "finance.sink.preview", {"sink": args.sink})
    if out is None:
        return 1
    assert isinstance(out, Preview)
    if not args.summary:
        emit(out.text)
        return 0
    setting = "true" if out.setting_enabled else "false"
    emit(
        Report(
            [
                f"{label}: {len(out.items)} item(s) pending, digest {out.digest[:8]};"
                " this preview sends nothing.",
                "Requests: " + (", ".join(out.requests) or "none") + ".",
                f"Setting finance.sinks.{args.sink}.enabled is {setting}.",
            ]
        )
    )
    return 0


def _cmd_disable(args: argparse.Namespace) -> int:
    from rheo_finance.sinks.ops import Recorded

    label = f"finance sink disable {args.sink}"
    out = _dispatch(label, "finance.sink.disable", {"sink": args.sink})
    if out is None:
        return 1
    assert isinstance(out, Recorded)
    emit(
        Report(
            [
                f"{label}: disabled; the next tick sends nothing to {args.sink}.",
                f"Recorded as decision {out.decision_id}.",
                f"Turning it on again takes `finance sink approve {args.sink}`.",
            ]
        )
    )
    return 0


def add_parser(
    commands: argparse._SubParsersAction[argparse.ArgumentParser],
    *,
    approve: Handler,
) -> None:
    parser = commands.add_parser(
        "sink",
        help="show, preview, approve or disable the outbound sinks",
        description=(
            "Every sink is off until the owner approves it in a terminal and sets its"
            " finance.sinks.<sink>.enabled setting to true."
        ),
    )
    sub = parser.add_subparsers(
        dest="sink_command", metavar="<sink command>", required=True
    )
    status = sub.add_parser("status", help="each sink's setting and approval")
    status.set_defaults(handler=_cmd_status)

    preview = sub.add_parser(
        "preview",
        help="what a sink would send or request; sends nothing",
        description="Print what the sink would send or request now. Sends nothing.",
    )
    preview.add_argument("sink", choices=SINKS)
    preview.add_argument(
        "--summary",
        action="store_true",
        help="print only the item count, the request list, digest8 and the setting",
    )
    preview.set_defaults(handler=_cmd_preview)

    approve_parser = sub.add_parser(
        "approve",
        help="approve a sink (interactive, in a terminal only)",
        description=(
            "Print the sink's preview and digest, then record an approval when you"
            " type `approve <sink> <digest8>`. Runs only in a terminal. It does not"
            " set the enabled setting."
        ),
    )
    approve_parser.add_argument("sink", choices=SINKS)
    approve_parser.set_defaults(handler=approve)

    disable = sub.add_parser(
        "disable",
        help="disable a sink (records a decision; no terminal needed)",
    )
    disable.add_argument("sink", choices=SINKS)
    disable.set_defaults(handler=_cmd_disable)
