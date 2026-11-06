"""Terminal-only restricted-name drafts, for the owner at their own terminal."""

from __future__ import annotations

import argparse
import csv
import io

from rheo_finance.render import Report, emit, emit_error, in_terminal


def _csv_row(values: tuple[str, ...]) -> str:
    buffer = io.StringIO(newline="")
    csv.writer(buffer, lineterminator="\n").writerow(values)
    return buffer.getvalue()


def _cmd_draft(_args: argparse.Namespace) -> int:
    if not in_terminal():
        emit_error(
            Report(
                ["finance alias draft: refused. stdin and stdout must be terminals."]
            )
        )
        return 1

    from rheo_finance import composition, operations
    from rheo_finance.alias import DraftOut
    from rheo_finance.contracts import Role
    from rheo_finance.manual.headers import MANUAL_FILES

    try:
        ctx = composition.build_context(Role.OWNER)
        out = operations.dispatch("finance.alias.draft", {}, Role.OWNER, ctx=ctx)
        assert isinstance(out, DraftOut)
        emit(
            Report(
                [
                    f"finance alias draft: {len(out.names)} unmatched names;"
                    " nothing written.",
                    "Choose party, slug and label before pasting."
                    " Matching uses stored payee patterns.",
                ]
            )
        )
        emit("party_aliases.csv")
        emit(_csv_row(MANUAL_FILES["party_aliases.csv"]))
        for name in out.names:
            emit(_csv_row(("", "payer_name", name)))
        emit("payees.csv")
        emit(_csv_row(MANUAL_FILES["payees.csv"]))
        for name in out.names:
            emit(_csv_row(("", "", "person", name, "", "")))
    except operations.OperationError as exc:
        emit_error(Report([f"finance alias draft: refused. {exc}"]))
        return 1
    except Exception as exc:
        # Never echo restricted values from validation, SQL or stream exceptions.
        emit_error(Report([f"finance alias draft: failed. {type(exc).__name__}"]))
        return 1
    return 0


def add_parser(commands: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = commands.add_parser(
        "alias", help="manual alias drafts (owner terminal only)"
    )
    subcommands = parser.add_subparsers(dest="alias_command", required=True)
    draft = subcommands.add_parser("draft", help="print unmatched names; write nothing")
    draft.set_defaults(handler=_cmd_draft)
