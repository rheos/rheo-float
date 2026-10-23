"""The ledger's content digest (FR-5, AC-5): one sha256 over the canonical rows of
every derived table.

Which tables: every table a derive clears or rewrites (``run.CLEARED`` and
``run.REWRITTEN``) and the registry the file operation keeps (``run.FILE_OWNED``:
``document`` and ``archive_skip``), since a rebuild registers the archive again and
parsing writes each document's status. The state tables (``run.STATE``: decisions,
their items and their targets) are inputs, not derived, and are left out; so are the
report views, which only select from the tables.

Which columns: all of them but ``LEFT_OUT``, the times a row was written
(``sink_spec.computed_at``, ``document.imported_at``) and the parse cache's key
(``document.parse_inputs``, bookkeeping about how a parse was reached, not what it
holds). ``occurred_at`` on a line or a txn is when the movement happened, data like any
date, and stays in.

Canonical rows: each value is tagged and rendered the way derived ids hash their
natural keys (``storage.ids.canonical_part``: numbers normalized, so ``1.50`` and
``1.5`` are one value; dates and times ISO, times in UTC; strings NFC), a list item by
item and a JSON object by sorted key. A row lists its primary key columns first, and
rows are hashed in the order of their canonical text, so a table with an ``id`` is in
id order and the digest never depends on the order the database returns rows in.
Nothing per run (a clock reading, a random id, a parser's unstored side output) is in
any digested column, which is what lets two runs over one document set agree.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final

from sqlalchemy import Table, select

from rheo_finance.context import UnitOfWork
from rheo_finance.derive.run import CLEARED, FILE_OWNED, REWRITTEN
from rheo_finance.storage.ids import canonical_part
from rheo_finance.storage.schema import metadata

LEFT_OUT: Final[Mapping[str, frozenset[str]]] = {
    "sink_spec": frozenset({"computed_at"}),
    "document": frozenset({"imported_at", "parse_inputs"}),
}


def digested_tables() -> list[Table]:
    """Every table the digest covers, by name."""
    names = {table.name for table in CLEARED} | REWRITTEN | FILE_OWNED
    return sorted(
        (table for table in metadata.tables.values() if table.name in names),
        key=lambda table: table.name,
    )


def _part(value: object) -> object:
    if isinstance(value, list | tuple):
        return ["list", [_part(each) for each in value]]
    if isinstance(value, dict):
        return ["map", [[str(key), _part(value[key])] for key in sorted(value)]]
    if isinstance(value, float):
        # Only a JSON number inside ``event.data`` can arrive as a float.
        return ["float", repr(value)]
    return canonical_part(value)


def _columns(table: Table) -> list[str]:
    left_out = LEFT_OUT.get(table.name, frozenset())
    keys = [column.name for column in table.primary_key.columns]
    rest = [column.name for column in table.columns if column.name not in keys]
    return [name for name in (*keys, *rest) if name not in left_out]


def canonical_rows(uow: UnitOfWork, table: Table) -> list[str]:
    """Every row of ``table`` as canonical text, in digest order."""
    names = _columns(table)
    rows = uow.execute(select(*(table.c[name] for name in names)))
    return sorted(
        json.dumps(
            [[name, _part(value)] for name, value in zip(names, row, strict=True)],
            ensure_ascii=False,
            separators=(",", ":"),
        )
        for row in rows
    )


@dataclass(frozen=True)
class LedgerDigest:
    digest: str
    # Rows per digested table, by table name.
    counts: Mapping[str, int]


def ledger_digest(uow: UnitOfWork) -> LedgerDigest:
    """sha256 over every digested table: its name and row count, then its rows."""
    hasher = hashlib.sha256()
    counts: dict[str, int] = {}
    for table in digested_tables():
        rows = canonical_rows(uow, table)
        counts[table.name] = len(rows)
        hasher.update(json.dumps([table.name, len(rows)]).encode("utf-8") + b"\n")
        for row in rows:
            hasher.update(row.encode("utf-8") + b"\n")
    return LedgerDigest(digest=hasher.hexdigest(), counts=counts)
