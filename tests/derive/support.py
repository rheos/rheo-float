"""A small ledger for the derive tests: a manual directory, an archive, and ``derive``.

Every value is synthetic: the account identifiers are the fixtures' fake numbers, and a
"PDF" is layout text read back by a stub extractor (``read_as_text``).
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import hashlib
import uuid
from collections.abc import Callable
from pathlib import Path

from sqlalchemy import Connection, select

from rheo_finance.context import FinanceContext, FinancePaths, UnitOfWork
from rheo_finance.contracts import Role
from rheo_finance.derive.run import Derived, run
from rheo_finance.documents.registry import document_id, file_archive
from rheo_finance.manual.accounts import ACCOUNTS_FILE, IDENTIFIERS_FILE
from rheo_finance.manual.cash_lines import TRANSACTIONS_FILE
from rheo_finance.manual.headers import MANUAL_FILES
from rheo_finance.settings import parse_settings
from rheo_finance.storage.schema import txn

NOW = dt.datetime(2026, 2, 20, 12, 0, tzinfo=dt.UTC)
FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "synthetic"
CSV_IDENTIFIER = "999000000077-999-99901-99902"
STATEMENT_IDENTIFIER = "999000000022"
D = dt.date

MakeContext = Callable[[Role], FinanceContext]


def read_as_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def csv_row(day: str, description: str, *, out: str = "", into: str = "",
            balance: str) -> str:  # fmt: skip
    """One headerless Vancity chequing row of the fixtures' fake account."""
    return f'{CSV_IDENTIFIER},{day},"{description}",,{out},{into},{balance}'


class Ledger:
    """One account registry, one archive, one derive over them."""

    def __init__(self, conn: Connection, ctx: FinanceContext, tmp_path: Path) -> None:
        self.conn = conn
        self.uow = UnitOfWork(conn)
        self.data = tmp_path / "data"
        self.archive = tmp_path / "archive"
        self.archive.mkdir()
        self.ctx = dataclasses.replace(
            ctx,
            settings=parse_settings(
                f'[finance.paths]\narchive = "{self.archive}"\n'
                f'downloads = "{tmp_path / "downloads-unset"}"\n'
            ),
            paths=FinancePaths(data=self.data),
        )

    # --- manual files -----------------------------------------------------------------

    def manual(self, name: str, *rows: str) -> None:
        folder = self.data / "manual"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / name).write_text(
            ",".join(MANUAL_FILES[name]) + "\n" + "".join(f"{r}\n" for r in rows),
            encoding="utf-8",
        )

    def accounts(self, *rows: tuple[str, str, str, str]) -> None:
        """Accounts as ``(slug, institution, kind, identifier)``, currency CAD. The
        identifier is a ``number`` for a chequing account, else a ``masked_card``."""
        self.manual(
            ACCOUNTS_FILE,
            *(f"{slug},{slug.title()},{inst},{kind},CAD,monthly,,,,,,,,,"
              for slug, inst, kind, _ in rows),
        )  # fmt: skip
        self.manual(
            IDENTIFIERS_FILE,
            *(f"{slug},{ident},number,," for slug, _, _, ident in rows),
        )

    def chequing(self, slug: str = "chequing") -> None:
        """The one account the CSV and statement fixtures name, under Vancity."""
        self.accounts((slug, "Vancity", "chequing", CSV_IDENTIFIER))
        self.add_identifier(slug, STATEMENT_IDENTIFIER)

    def add_identifier(self, slug: str, identifier: str) -> None:
        path = self.data / "manual" / IDENTIFIERS_FILE
        with path.open("a", encoding="utf-8") as handle:
            handle.write(f"{slug},{identifier},number,,\n")

    def cash(self, *rows: str) -> None:
        self.manual(TRANSACTIONS_FILE, *rows)

    # --- documents --------------------------------------------------------------------

    def file(self, relative: str, content: str | bytes) -> uuid.UUID:
        data = content.encode("utf-8") if isinstance(content, str) else content
        target = self.archive / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        file_archive(self.uow, self.archive, self.data, NOW)
        return document_id(hashlib.sha256(data).hexdigest())

    def derive(self) -> Derived:
        return run(self.uow, self.ctx, extract_pdf=read_as_text)

    # --- reading back -----------------------------------------------------------------

    def txns(self) -> list[dict[str, object]]:
        """Every txn, oldest first, as column dicts."""
        rows = self.conn.execute(
            select(txn).order_by(txn.c.posting_date, txn.c.description_norm, txn.c.id)
        ).mappings()
        return [dict(row) for row in rows]
