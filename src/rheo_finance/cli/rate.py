"""``finance rate fetch [--from D] [--to D]`` (FR-14).

Dispatches ``finance.rate.fetch`` as the owner. The operation asks the ``rates`` sink
gate first, so while the sink is off this sends nothing and says so (exit 1). Output is
counts, series ids and dates only. Like every module ``cli.main`` registers, this one
keeps sqlalchemy and storage out of its top-level imports.
"""

from __future__ import annotations

import argparse
import datetime as dt

from rheo_finance.render import Report, as_of, emit, emit_error


def _day(value: str) -> dt.date:
    try:
        return dt.date.fromisoformat(value)
    except ValueError:
        raise argparse.ArgumentTypeError("a date is YYYY-MM-DD") from None


def _cmd_fetch(args: argparse.Namespace) -> int:
    from rheo_finance import composition, operations
    from rheo_finance.contracts import Role
    from rheo_finance.rates.valet import FetchOut

    label = "finance rate fetch"
    ctx = composition.build_context(Role.OWNER)
    payload = {"from_date": args.from_date, "to_date": args.to_date}
    try:
        out = operations.dispatch("finance.rate.fetch", payload, Role.OWNER, ctx=ctx)
    except operations.OperationError as exc:
        emit_error(Report([f"{label}: refused. {exc}"]))
        return 1
    assert isinstance(out, FetchOut)
    if not out.enabled:
        emit_error(
            Report(
                [
                    f"{label}: the rates sink is off; nothing was sent.",
                    f"{out.planned} series would be requested;"
                    " `finance sink preview rates` lists them.",
                ]
            )
        )
        return 1
    stamp = as_of(ctx.clock, ctx.settings.timezone)
    detail = [
        f"{each.series} ({each.currency}) {each.start_date.isoformat()} to"
        f" {each.end_date.isoformat()}: "
        + (
            f"stored {each.stored} of {each.received} received."
            if each.outcome == "stored"
            else f"failed, {each.reason}."
        )
        for each in out.series
    ]
    emit(
        Report(
            [
                f"{label}: {out.requests} request(s) sent, {out.stored} rate(s)"
                f" stored, {out.failed} failed.",
                stamp,
            ],
            detail,
        )
    )
    return 1 if out.failed else 0


def add_parser(commands: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = commands.add_parser(
        "rate",
        help="fetch exchange rates (rates sink)",
        description="Exchange rates for converting the ledger's lines to CAD.",
    )
    sub = parser.add_subparsers(
        dest="rate_command", metavar="<rate command>", required=True
    )
    fetch = sub.add_parser(
        "fetch",
        help="fetch the Bank of Canada rates the lines lack",
        description=(
            "Fetch the Bank of Canada rates the ledger's lines lack, one request per"
            " series. Sends nothing while the rates sink is off."
        ),
    )
    fetch.add_argument(
        "--from", dest="from_date", type=_day, help="first date to request"
    )
    fetch.add_argument("--to", dest="to_date", type=_day, help="last date to request")
    fetch.set_defaults(handler=_cmd_fetch)
