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

from rheo_finance import db

Handler = Callable[[argparse.Namespace], int]

HEADLINE = "finance: Float, a local-first personal finance ledger."


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="finance",
        description=HEADLINE,
    )
    commands = parser.add_subparsers(
        dest="command", metavar="<command>", title="commands"
    )
    db.add_parser(commands)
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
