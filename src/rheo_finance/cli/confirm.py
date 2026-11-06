"""Interactive confirm and explicit answers over owner-dispatched operations."""

from __future__ import annotations

import argparse
import sys
from typing import TYPE_CHECKING

from rheo_finance.cli.ledger import _context, _dispatch, _run
from rheo_finance.render import Report, emit, money

if TYPE_CHECKING:
    from rheo_finance.context import FinanceContext
    from rheo_finance.queue import Item, ListOut

LABEL = "finance confirm"


def _queue(
    ctx: FinanceContext, *, orphans: bool = False, counts: bool = False
) -> ListOut:
    from rheo_finance.queue import ListOut

    out = _dispatch(
        LABEL, "finance.queue.list", ctx, {"orphans": orphans, "counts_only": counts}
    )
    assert isinstance(out, ListOut)
    return out


def _answer(ctx: FinanceContext, code: str, answer: str) -> None:
    from rheo_finance.queue import AnswerOut

    out = _dispatch(
        LABEL, "finance.confirm.answer", ctx, {"code": code, "answer": answer}
    )
    assert isinstance(out, AnswerOut)
    # Only a successful answer reaches this dispatch. The answer committed first,
    # so derive sees it and the next read reflects it. No derive on a refusal.
    _dispatch(LABEL, "finance.ledger.derive", ctx)
    emit(Report([f"{LABEL}: {out.recorded} decision(s) recorded; ledger derived."]))


def _show(item: Item) -> None:
    lines = [*item.evidence]
    lines.extend(f"  {choice.answer}: {choice.label}" for choice in item.choices)
    if item.recommendation is not None:
        lines.append(f"Recommendation: {item.recommendation}.")
    lines.append(
        "Enter an answer, a category for a category item, or skip. Blank skips."
    )
    emit(
        Report(
            [f"{item.code} {item.kind}: {money(item.amount_at_stake, 'CAD')} at stake"],
            lines,
        )
    )


def _cmd_confirm(args: argparse.Namespace) -> int:
    ctx = _context()
    out = _queue(ctx, orphans=args.orphans, counts=args.counts)
    if args.counts:
        emit(
            Report(
                [f"{LABEL}: {out.total} item(s) pending."],
                [f"{each.kind}: {each.count}" for each in out.counts],
            )
        )
        return 0
    emit(
        Report(
            [
                f"{LABEL}: {out.total} item(s) pending; "
                f"at most {out.batch_size} in this batch."
            ]
        )
    )
    shown: set[str] = set()
    for _ in range(out.batch_size):
        item = next((each for each in out.items if each.code not in shown), None)
        if item is None:
            break
        shown.add(item.code)
        _show(item)
        emit("Answer:")
        answer = sys.stdin.readline()
        if not answer:
            break
        if answer.strip().lower() not in ("", "skip"):
            _answer(ctx, item.code, answer.strip())
        out = _queue(ctx, orphans=args.orphans)
    return 0


def _cmd_answer(args: argparse.Namespace) -> int:
    _answer(_context(), args.code, args.answer)
    return 0


def add_parser(commands: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = commands.add_parser(
        "confirm", help="review current confirm items or answer a code"
    )
    parser.add_argument("--counts", action="store_true", help="per-kind counts only")
    parser.add_argument(
        "--orphans", action="store_true", help="review re-attachment items only"
    )
    parser.set_defaults(handler=_run(_cmd_confirm))
    group = parser.add_subparsers(dest="confirm_command", metavar="<confirm command>")
    answer = group.add_parser(
        "answer", help="recheck a code, record its answer, and derive once"
    )
    answer.add_argument("code")
    answer.add_argument("answer")
    answer.set_defaults(handler=_run(_cmd_answer))
