"""``finance decision reverse <id>`` and ``finance decision reverse --bulk <id>``.

Registered by ``cli.main``, so this module keeps sqlalchemy and storage out of its
top-level imports: the handler loads the operation layer when it runs.
"""

from __future__ import annotations

import argparse
import uuid

from rheo_finance.render import Report, emit, emit_error

LABEL = "finance decision reverse"


def _cmd_reverse(args: argparse.Namespace) -> int:
    from rheo_finance import composition, decision, operations
    from rheo_finance.contracts import Role

    payload = (
        decision.ReverseIn(bulk_id=args.bulk)
        if args.bulk is not None
        else decision.ReverseIn(decision_id=args.decision_id)
    )
    try:
        out = operations.dispatch(
            "finance.decision.reverse",
            payload,
            Role.OWNER,
            ctx=composition.build_context(Role.OWNER),
        )
    except operations.OperationError as exc:
        emit_error(Report([f"{LABEL}: refused. {exc}"]))
        return 1
    assert isinstance(out, decision.ReverseOut)
    if args.bulk is not None:
        headline = [
            f"{LABEL}: reversed bulk run {args.bulk};"
            f" {out.reversed} decision(s) no longer apply."
        ]
    else:
        headline = [f"{LABEL}: reversed decision {args.decision_id}."]
    headline.append(f"Recorded as decision {out.decision_id}.")
    emit(Report(headline))
    return 0


def add_parser(commands: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = commands.add_parser(
        "decision",
        help="reverse a decision or a bulk run",
        description="Work with recorded decisions. Decisions are never edited.",
    )
    sub = parser.add_subparsers(
        dest="decision_command", metavar="<decision command>", required=True
    )
    reverse = sub.add_parser(
        "reverse",
        help="reverse one decision, or every decision of a bulk run",
        description=(
            "Record a reversal. The reversed decision stays in the history and"
            " stops applying; totals re-derive from the reversal."
        ),
    )
    target = reverse.add_mutually_exclusive_group(required=True)
    target.add_argument(
        "decision_id", nargs="?", type=uuid.UUID, help="the decision to reverse"
    )
    target.add_argument(
        "--bulk",
        type=uuid.UUID,
        metavar="BULK_ID",
        help="reverse every decision of this bulk run",
    )
    reverse.set_defaults(handler=_cmd_reverse)
