"""Stable manual-file line references from the existing reader operation."""

from __future__ import annotations

import argparse

from rheo_finance import db
from rheo_finance.render import Report, emit, emit_error, iso_date


def _cmd_lines(args: argparse.Namespace) -> int:
    from rheo_finance import composition, operations
    from rheo_finance.contracts import Role
    from rheo_finance.reports.lines import ListOut

    try:
        ctx = composition.build_context(Role.OWNER)
        out = operations.dispatch(
            "finance.txn.list",
            {
                "account": args.account,
                "date_from": args.date_from,
                "date_to": args.date_to,
                "state": args.state,
            },
            Role.OWNER,
            ctx=ctx,
        )
        assert isinstance(out, ListOut)
        detail: list[str] = []
        for row in out.rows:
            states = [
                f"{label}={value}"
                for label, value in (
                    ("transfer", row.transfer_state),
                    ("pair", row.pair_kind),
                    ("allocation", row.allocation_state),
                    ("exclusion", row.exclusion),
                    ("decision", row.decision_state),
                    ("split", row.split_state),
                )
                if value is not None
            ]
            if row.provisional:
                states.append("provisional")
            # Native quantities remain exact, including sub-cent fiat source values.
            detail.append(
                f"{row.line_ref} | {row.account_label} | {iso_date(row.posting_date)}"
                f" | {row.amount:f} {row.currency} | {row.display_label}"
                f" | {'; '.join(states) or 'no states'}"
            )
        emit(Report([f"finance lines: {len(out.rows)} lines"], detail or ["no lines"]))
    except operations.OperationError as exc:
        emit_error(Report([f"finance lines: refused. {exc}"]))
        return 1
    except Exception as exc:
        # Reader failures must not echo bound parameters or output model values.
        message = db.UNREACHABLE if db.unreachable(exc) else type(exc).__name__
        emit_error(Report([f"finance lines: failed. {message}"]))
        return 1
    return 0


def add_parser(commands: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = commands.add_parser(
        "lines", help="manual-file line refs (reader connection)"
    )
    parser.add_argument("--account", metavar="A", help="account slug")
    parser.add_argument(
        "--from", dest="date_from", metavar="D", help="first posting date"
    )
    parser.add_argument("--to", dest="date_to", metavar="D", help="last posting date")
    parser.add_argument("--state", metavar="S", help="line state or annotation")
    parser.set_defaults(handler=_cmd_lines)
