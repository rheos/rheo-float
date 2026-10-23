"""Local AC-7 parity check: the ledger against the existing Vancity and Virgin parsers.

Usage: ``uv run python scripts/parity_check.py <directory>``, where the directory holds
the existing monitors' ``vancity.csv`` and ``virgin-plus.csv``.

It reads the ledger's per-statement totals through ``finance.statement.list`` (a READ
operation, dispatched as the owner on the live database the ``finance`` commands use)
and compares them with the CSVs by ``rheo_finance.parity.compare``; the rule and the
result kinds are in that module's docstring. It prints counts, then one line per
statement with its date and result, never an amount, an identifier or a name. Exits 0
when no statement fails, 1 when one does or the check cannot run, 2 on a usage error.
Running it on the real outputs is a local gate; the repo holds no real data.
"""

from __future__ import annotations

import sys
from pathlib import Path

from rheo_finance import db, parity
from rheo_finance.render import Report, emit, emit_error, failure_line

LABEL = "parity"
USAGE = "usage: parity_check.py <directory holding vancity.csv and virgin-plus.csv>"


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1 or args[0].startswith("-"):
        emit_error(Report([USAGE]))
        return 2
    try:
        reference = parity.read_reference(Path(args[0]))
    except parity.ParityInputError as exc:
        emit_error(Report([f"{LABEL}: cannot read the reference. {exc}"]))
        return 1

    from rheo_finance import catalogue, composition, operations
    from rheo_finance.contracts import Role
    from rheo_finance.derive.ops import StatementListOut

    catalogue.load_all()
    payload = {"parsers": [parity.VANCITY_PARSER, parity.VIRGIN_PARSER]}
    try:
        out = operations.dispatch(
            "finance.statement.list",
            payload,
            Role.OWNER,
            ctx=composition.build_context(Role.OWNER),
        )
    except operations.OperationError as exc:
        emit_error(Report([f"{LABEL}: refused. {exc}"]))
        return 1
    except Exception as exc:
        line = f"{LABEL}: {db.UNREACHABLE}" if db.unreachable(exc) else None
        emit_error(Report([line or failure_line(LABEL, exc)]))
        return 1
    assert isinstance(out, StatementListOut)

    results = parity.compare(parity.ledger_statements(out), reference)
    emit(parity.report(results))
    return 1 if any(each.kind.failed for each in results) else 0


if __name__ == "__main__":
    sys.exit(main())
