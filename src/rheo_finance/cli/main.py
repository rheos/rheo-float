"""Entry point for the ``finance`` console script.

Subcommands are registered here by later prompts. Each one sets ``handler`` on its
subparser with ``set_defaults(handler=...)``; ``main`` dispatches to it.

This module never imports sqlalchemy or ``rheo_finance.storage``: ``finance --help``
and argument errors must work without the database layer loaded. Handlers import
what they need inside their own modules.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable
from typing import TYPE_CHECKING

from rheo_finance import db, init_data
from rheo_finance.render import emit

if TYPE_CHECKING:
    from _typeshed import SupportsWrite

Handler = Callable[[argparse.Namespace], int]

HEADLINE = "finance: Float, a local-first personal finance ledger."


class GuardedParser(argparse.ArgumentParser):
    """An ArgumentParser whose help, usage and error text goes through ``render.emit``.

    argparse writes every message through ``_print_message``; subparsers inherit this
    class, so ``finance <command> --help`` is guarded too. Like argparse, it ignores
    an OSError from a closed stream (``finance --help | head -1``).
    """

    def _print_message(
        self, message: str, file: SupportsWrite[str] | None = None
    ) -> None:
        if message:
            try:
                emit(message, file or sys.stderr)
            except OSError:
                pass


def build_parser() -> argparse.ArgumentParser:
    parser: argparse.ArgumentParser = GuardedParser(
        prog="finance",
        description=HEADLINE,
    )
    commands = parser.add_subparsers(
        dest="command", metavar="<command>", title="commands"
    )
    db.add_parser(commands)
    init_data.add_parser(commands)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    except SystemExit as exc:
        # argparse exits 0 after --help and 2 on a usage error; return the code
        # so callers (and tests) get it without a raised SystemExit. A None code
        # means success, as it does for sys.exit().
        if exc.code is None:
            return 0
        return exc.code if isinstance(exc.code, int) else 2
    handler: Handler | None = getattr(args, "handler", None)
    if handler is None:
        parser.print_help()
        return 0
    return handler(args)


if __name__ == "__main__":
    sys.exit(main())
