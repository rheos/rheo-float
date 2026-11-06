"""The finance report command family. Imports storage only inside its handler."""

from __future__ import annotations

import argparse

from rheo_finance.render import Report, emit, emit_error, money


def _cmd_open_items(args: argparse.Namespace) -> int:
    from rheo_finance import composition, operations
    from rheo_finance.contracts import Role
    from rheo_finance.reports.open_items import ListOut

    ctx = composition.build_context(Role.OWNER)
    try:
        out = operations.dispatch(
            "finance.openitem.list", {"year": args.year}, Role.OWNER, ctx=ctx
        )
    except operations.OperationError as exc:
        emit_error(Report([f"finance report open-items: refused. {exc}"]))
        return 1
    assert isinstance(out, ListOut)
    if not out.items:
        emit(Report([f"{args.year}: no open items"]))
        return 0
    summaries = [
        f"{row.kind}: {row.count}; "
        + (
            ", ".join(
                f"in {money(sums.in_amount, sums.currency)}, "
                f"out {money(sums.out_amount, sums.currency)}"
                for sums in row.sums
            )
            or "no stored amount"
        )
        for row in out.summary
    ]
    # Two kinds per line keep all seven core kinds inside the six-line headline.
    headline = [
        f"{args.year}: {len(out.items)} open items",
        *(
            "; ".join(summaries[index : index + 2])
            for index in range(0, len(summaries), 2)
        ),
    ]
    detail = [
        f"{row.line_ref} {row.occurred_on.isoformat()} "
        f"{row.account_label or '(no account)'} "
        f"{row.direction or ''} "
        + (
            money(row.amount, row.currency)
            if row.amount is not None and row.currency is not None
            else "no stored amount"
        )
        + f"; {row.state}; {row.reason}"
        + (f"; category {row.category}" if row.category is not None else "")
        + (f"; tag {row.tag}" if row.tag is not None else "")
        + (
            "; candidates " + ", ".join(row.candidate_refs)
            if row.candidate_refs
            else ""
        )
        for row in out.items
    ]
    emit(Report(headline, detail))
    return 0


def add_parser(commands: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = commands.add_parser("report", help="read ledger reports")
    sub = parser.add_subparsers(dest="report_command", required=True)
    items = sub.add_parser("open-items", help="items left out of totals")
    items.add_argument("--year", type=int, required=True)
    items.set_defaults(handler=_cmd_open_items)
