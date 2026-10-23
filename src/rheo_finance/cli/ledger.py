"""``finance import``, ``rebuild``, ``digest``, ``coverage`` and ``status`` (FR-35,
FR-51, Plan Phase 2 deliverable 5).

Every command builds the production context once and dispatches as the owner; the
connection is the operation's own choice (Key decision 21). ``coverage`` and ``status``
dispatch only ``finance.coverage.get`` and ``finance.status.get``, which run on the
reader connection, so they work under the reader DSN alone.

When Postgres cannot be reached (``db.unreachable``), ``status`` prints the one line
"database unreachable" (its JSON form, that one line as line 1) and exits 0, so a
status check still answers; every other command prints the same words on stderr and
exits 1.

Output is counts, states, dates and digests only: no path, identifier, description or
amount. Like every module ``cli.main`` registers, this one keeps sqlalchemy and storage
out of its top-level imports.
"""

from __future__ import annotations

import argparse
import datetime as dt
from collections.abc import Callable, Sequence
from itertools import groupby
from typing import TYPE_CHECKING, Final

from rheo_finance import db
from rheo_finance.render import Report, as_of, emit, emit_error, emit_json

if TYPE_CHECKING:
    from pydantic import BaseModel

    from rheo_finance.context import FinanceContext
    from rheo_finance.derive.ops import Count, DeriveOut
    from rheo_finance.reports.ops import CoverageMonth

Handler = Callable[[argparse.Namespace], int]

# One letter per month in the coverage matrix; "." for a month the account has no row.
STATE_LETTERS: Final = {
    "have": "H",
    "partial": "P",
    "missing": "M",
    "rejected": "R",
    "unparsed": "U",
    "not_applicable": "-",
}
NO_MONTH: Final = "."
LEGEND: Final = (
    "Months January to December, one letter each: H have, P partial, M missing,"
    " R rejected, U unparsed, - not applicable, . no month."
)


class Stopped(Exception):
    """A command that already reported why it stopped; carries the exit code."""

    def __init__(self, code: int) -> None:
        super().__init__(code)
        self.code = code


def _context() -> FinanceContext:
    from rheo_finance import composition
    from rheo_finance.contracts import Role

    return composition.build_context(Role.OWNER)


def _dispatch(
    label: str, name: str, ctx: FinanceContext, payload: dict[str, object] | None = None
) -> BaseModel:
    """Dispatch ``name`` as the owner. A refusal or an unreachable database is
    reported on stderr and stops the command with exit 1."""
    from rheo_finance import operations
    from rheo_finance.contracts import Role

    try:
        return operations.dispatch(name, payload or {}, Role.OWNER, ctx=ctx)
    except operations.OperationError as exc:
        emit_error(Report([f"{label}: refused. {exc}"]))
        raise Stopped(1) from None
    except Exception as exc:
        if db.unreachable(exc):
            emit_error(Report([f"{label}: {db.UNREACHABLE}"]))
            raise Stopped(1) from None
        raise


def _run(command: Callable[[argparse.Namespace], int]) -> Handler:
    def handler(args: argparse.Namespace) -> int:
        try:
            return command(args)
        except Stopped as stop:
            return stop.code

    return handler


def _plural(count: int, one: str, many: str) -> str:
    return f"{count} {one if count == 1 else many}"


def _counts(counts: Sequence[Count]) -> str:
    return ", ".join(f"{each.count} {each.name.replace('_', ' ')}" for each in counts)


def _derived_lines(out: DeriveOut) -> list[str]:
    return [
        f"derive: {out.parsed_now} parsed now, {out.kept} kept;"
        f" {_plural(out.txns, 'ledger line', 'ledger lines')},"
        f" {_plural(out.discrepancies, 'source discrepancy', 'source discrepancies')}",
        "documents: " + (_counts(out.documents) or "none"),
        "account-months: " + (_counts(out.months) or "none"),
    ]


def _derived_detail(out: DeriveOut) -> list[str]:
    return [
        f"Additional sources: {out.additional_sources}.",
        f"Incomplete lines: {out.incomplete_lines}.",
        f"Input fingerprint: {out.fingerprint[:8]}.",
    ]


# --- finance import -----------------------------------------------------------------


def _cmd_import(_args: argparse.Namespace) -> int:
    from rheo_finance.derive.ops import DeriveOut
    from rheo_finance.documents.registry import FileOut

    label = "finance import"
    ctx = _context()
    filed = _dispatch(label, "finance.document.file", ctx)
    assert isinstance(filed, FileOut)
    derived = _dispatch(label, "finance.ledger.derive", ctx)
    assert isinstance(derived, DeriveOut)
    stamp = as_of(ctx.clock, ctx.settings.timezone)
    downloads = filed.downloads
    headline = [
        f"{label}: {_plural(filed.registered, 'new document', 'new documents')}"
        f" registered, {downloads.filed} filed from Downloads, {stamp}",
        *_derived_lines(derived),
    ]
    if downloads.scanned:
        intake = (
            f"Downloads: {downloads.filed} filed, {downloads.known} already known,"
            f" {downloads.refused} refused by the path check,"
            f" {downloads.unrecognised} not dated, {downloads.unreadable} unreadable;"
            f" {downloads.monitor_skipped} monitor files left to the monitors"
            f" ({downloads.monitor_waiting} waiting)."
        )
    else:
        intake = "Downloads: not scanned, the directory does not exist."
    skipped = sum(each.count for each in filed.skipped)
    detail = [
        intake,
        f"Archive: {filed.registered} registered, {filed.known} known,"
        f" {filed.moved} moved, {filed.duplicates} duplicate copies,"
        f" {filed.withheld} paths withheld, {skipped} files skipped.",
        *_derived_detail(derived),
    ]
    emit(Report(headline, detail))
    return 0


# --- finance rebuild ----------------------------------------------------------------


def _cmd_rebuild(_args: argparse.Namespace) -> int:
    from rheo_finance.derive.ops import RebuildOut

    label = "finance rebuild"
    ctx = _context()
    out = _dispatch(label, "finance.ledger.rebuild", ctx)
    assert isinstance(out, RebuildOut)
    stamp = as_of(ctx.clock, ctx.settings.timezone)
    headline = [
        f"{label}: every document parsed again,"
        f" {_plural(out.registered, 'new document', 'new documents')} registered,"
        f" {stamp}",
        *_derived_lines(out),
    ]
    emit(Report(headline, _derived_detail(out)))
    return 0


# --- finance digest -----------------------------------------------------------------


def _cmd_digest(_args: argparse.Namespace) -> int:
    from rheo_finance.derive.ops import DigestOut

    label = "finance digest"
    ctx = _context()
    out = _dispatch(label, "finance.ledger.digest", ctx)
    assert isinstance(out, DigestOut)
    rows = sum(each.count for each in out.tables)
    emit(
        Report(
            [
                f"{label}: {out.digest}",
                f"{_plural(rows, 'row', 'rows')} in"
                f" {_plural(len(out.tables), 'table', 'tables')},"
                f" {as_of(ctx.clock, ctx.settings.timezone)}",
            ],
            [f"{each.name}: {each.count}" for each in out.tables],
        )
    )
    return 0


# --- finance coverage ---------------------------------------------------------------


def _state_counts(months: Sequence[CoverageMonth]) -> dict[str, int]:
    counts = dict.fromkeys(STATE_LETTERS, 0)
    for month in months:
        counts[month.state] = counts.get(month.state, 0) + 1
    return counts


def _matrix(months: Sequence[CoverageMonth]) -> list[str]:
    """Per account and year: twelve month letters in quarters, then the months that
    carry a reason, consecutive ones with the same state and reason as one range."""
    lines: list[str] = []
    for _, account_months in groupby(months, key=lambda m: m.account):
        rows = list(account_months)
        label = rows[0].account_label
        for year, year_months in groupby(rows, key=lambda m: m.month.year):
            in_year = list(year_months)
            letters = [NO_MONTH] * 12
            for each in in_year:
                letters[each.month.month - 1] = STATE_LETTERS.get(each.state, "?")
            cells = " ".join("".join(letters[q : q + 3]) for q in range(0, 12, 3))
            lines.append(f"{label} {year}: {cells}")
            lines.extend(_reasons(in_year))
    return lines


def _reasons(months: Sequence[CoverageMonth]) -> list[str]:
    lines: list[str] = []
    runs: list[list[CoverageMonth]] = []
    for each in months:
        if each.reason is None:
            continue
        previous = runs[-1][-1] if runs else None
        if (
            previous is not None
            and (previous.state, previous.reason) == (each.state, each.reason)
            and _next_month(previous.month) == each.month
        ):
            runs[-1].append(each)
        else:
            runs.append([each])
    for run in runs:
        first, last = run[0].month, run[-1].month
        span = f"{first:%Y-%m}" if first == last else f"{first:%Y-%m} to {last:%Y-%m}"
        state = run[0].state.replace("_", " ")
        lines.append(f"  {span} {state}: {run[0].reason}")
    return lines


def _next_month(month: dt.date) -> dt.date:
    return dt.date(month.year + month.month // 12, month.month % 12 + 1, 1)


def _cmd_coverage(args: argparse.Namespace) -> int:
    from rheo_finance.reports.ops import CoverageOut

    label = "finance coverage"
    ctx = _context()
    out = _dispatch(label, "finance.coverage.get", ctx)
    assert isinstance(out, CoverageOut)
    stamp = as_of(ctx.clock, ctx.settings.timezone)
    counts = _state_counts(out.months)
    if args.json:
        emit_json(
            {
                "as_of": stamp,
                "months_by_state": counts,
                "no_data_accounts": out.no_data_accounts,
                "months": out.months,
            }
        )
        return 0
    headline = [
        f"{label}: {counts['have']} have, {counts['partial']} partial,"
        f" {counts['missing']} missing account-months, {stamp}",
        f"{counts['rejected']} rejected, {counts['unparsed']} unparsed,"
        f" {counts['not_applicable']} not applicable",
        f"{_plural(out.no_data_accounts, 'account', 'accounts')} with no data",
    ]
    detail = [LEGEND, *_matrix(out.months)] if out.months else []
    emit(Report(headline, detail))
    return 0


# --- finance status -----------------------------------------------------------------


def _cmd_status(args: argparse.Namespace) -> int:
    from rheo_finance import operations
    from rheo_finance.contracts import Role
    from rheo_finance.reports.ops import StatusLine, StatusOut

    ctx = _context()
    try:
        out = operations.dispatch("finance.status.get", {}, Role.OWNER, ctx=ctx)
    except operations.OperationError as exc:
        emit_error(Report([f"finance status: refused. {exc}"]))
        return 1
    except Exception as exc:
        if not db.unreachable(exc):
            raise
        out = StatusOut(lines=[StatusLine(line_no=1, text=db.UNREACHABLE)])
    assert isinstance(out, StatusOut)
    if args.json:
        emit_json(out)
    else:
        emit(Report.from_status_lines([each.text for each in out.lines]))
    return 0


# --- the parsers ----------------------------------------------------------------------


def add_parser(commands: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    importing = commands.add_parser(
        "import",
        help="file new Downloads, register new archive files, then derive",
        description=(
            "Copy dateable new files from Downloads into the archive, register every"
            " new archive file, then derive the ledger. Prints counts only."
        ),
    )
    importing.set_defaults(handler=_run(_cmd_import))

    rebuild = commands.add_parser(
        "rebuild",
        help="parse every document again and rebuild the derived tables",
        description=(
            "Register the archive again, parse every document again and rebuild every"
            " derived table. Decisions are kept. Prints counts only."
        ),
    )
    rebuild.set_defaults(handler=_run(_cmd_rebuild))

    digest = commands.add_parser(
        "digest",
        help="the ledger digest and the row count of each derived table",
    )
    digest.set_defaults(handler=_run(_cmd_digest))

    coverage = commands.add_parser(
        "coverage",
        help="have, partial and missing months per account (reader connection)",
    )
    coverage.add_argument("--json", action="store_true", help="print JSON")
    coverage.set_defaults(handler=_run(_cmd_coverage))

    status = commands.add_parser(
        "status",
        help="the fixed status list, at most 15 lines (reader connection)",
    )
    status.add_argument("--json", action="store_true", help="print JSON")
    status.set_defaults(handler=_run(_cmd_status))
