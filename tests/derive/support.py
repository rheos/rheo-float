"""A small ledger for the derive tests: a manual directory, an archive, and ``derive``.

Every value is synthetic: the account identifiers are the fixtures' fake numbers, and a
"PDF" is layout text read back by a stub extractor (``read_as_text``).

The card fixtures (``card_*``) use a small synthetic layout read by ``parse_card``, a
test-only parser registered under a test-only issuer directory (``CARD_ISSUER``) by the
``card_parsers`` fixture. They carry the overlap pattern of a card archive, not any
issuer's layout.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import hashlib
import uuid
from collections.abc import Callable
from decimal import Decimal
from pathlib import Path

from sqlalchemy import Connection, select

from rheo_finance.context import FinanceContext, FinancePaths, UnitOfWork
from rheo_finance.contracts import Role
from rheo_finance.derive.run import Derived, run
from rheo_finance.documents.registry import document_id, file_archive
from rheo_finance.manual.accounts import ACCOUNTS_FILE, IDENTIFIERS_FILE
from rheo_finance.manual.cash_lines import TRANSACTIONS_FILE
from rheo_finance.manual.headers import MANUAL_FILES
from rheo_finance.parsers.base import (
    AccountLookup,
    NotRecognised,
    ParsedLine,
    ParsedPeriod,
    ParseResult,
    Rejected,
)
from rheo_finance.parsers.registry import Parser
from rheo_finance.settings import parse_settings
from rheo_finance.storage.schema import txn

NOW = dt.datetime(2026, 2, 20, 12, 0, tzinfo=dt.UTC)
FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "synthetic"
CSV_IDENTIFIER = "999000000077-999-99901-99902"
STATEMENT_IDENTIFIER = "999000000022"
D = dt.date

MakeContext = Callable[[Role], FinanceContext]

CARD_ISSUER = "synthetic-card"
CARD_INSTITUTION = "Sample Card"
CARD_IDENTIFIER = "999000000033"
CARD_STATEMENT = "SYNTHETIC CARD STATEMENT"
CARD_TRANSACTIONS = "SYNTHETIC CARD TRANSACTIONS"


def card_fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def parse_card(text: str, accounts: AccountLookup) -> ParseResult:
    """The synthetic card layout: a header (statement or transactions), ``ACCOUNT``,
    an optional ``CURRENCY`` (CAD when absent), for a statement ``PERIOD``,
    ``OPENING`` and ``CLOSING``, then one ``LINE`` per transaction as posting date,
    transaction date (``-`` for none), signed amount and description. A transactions
    document's period runs from its earliest line's date to its latest, with control
    ``none``."""
    rows = [row.strip() for row in text.splitlines() if row.strip()]
    if not rows or rows[0] not in (CARD_STATEMENT, CARD_TRANSACTIONS):
        raise NotRecognised
    statement = rows[0] == CARD_STATEMENT
    fields: dict[str, str] = {}
    found: list[tuple[dt.date, dt.date | None, Decimal, str]] = []
    for row in rows[1:]:
        head, _, rest = row.partition(" ")
        if head == "LINE":
            posting, txn_date, amount, description = rest.split(" ", 3)
            found.append(
                (
                    dt.date.fromisoformat(posting),
                    None if txn_date == "-" else dt.date.fromisoformat(txn_date),
                    Decimal(amount),
                    description,
                )
            )
        else:
            fields[head] = rest
    if statement:
        first, last = (dt.date.fromisoformat(d) for d in fields["PERIOD"].split())
    else:
        first = min(line[0] for line in found)
        last = max(line[0] for line in found)
    matched = accounts.by_identifier(CARD_INSTITUTION, fields["ACCOUNT"], first, last)
    if len(matched) != 1:
        raise Rejected("unknown_account", (first, last))
    account = matched[0]
    currency = fields.get("CURRENCY", "CAD")
    period = ParsedPeriod(
        account=account,
        currency=currency,
        period_start=first,
        period_end=last,
        control_kind="summary" if statement else "none",
        opening=Decimal(fields["OPENING"]) if statement else None,
        closing=Decimal(fields["CLOSING"]) if statement else None,
    )
    return ParseResult(
        periods=(period,),
        lines=tuple(
            ParsedLine(
                line_kind="transaction",
                account=account,
                posting_date=posting,
                txn_date=txn_date,
                amount=amount,
                currency=currency,
                description_raw=description,
            )
            for posting, txn_date, amount, description in found
        ),
        control_kind=period.control_kind,
    )


# One parser per format the card fixtures are filed as: a PDF (statement or transaction
# download), a CSV, and a text file standing for any other format.
CARD_PARSERS = tuple(
    Parser(f"synthetic_card_{fmt}", CARD_ISSUER, fmt, "1", parse_card)
    for fmt in ("pdf", "csv", "txt")
)


# The conversion fixtures: one account at a test-only institution, and a layout whose
# lines carry what stage 4 reads (a currency, an issuer-stated CAD amount, a foreign
# original), read by ``parse_fx`` under the test-only issuer directory ``FX_ISSUER``.
FX_ISSUER = "synthetic-fx"
FX_INSTITUTION = "Sample FX"
FX_IDENTIFIER = "999000000044"
FX_LINES = "SYNTHETIC FX LINES"


def parse_fx(text: str, accounts: AccountLookup) -> ParseResult:
    """``FX_LINES``, ``CURRENCY <code>``, an optional ``ACCOUNT <identifier>``
    (``FX_IDENTIFIER`` when absent), then ``LINE <posting date> <signed amount>
    <stated CAD or -> <original currency or -> <description>``. The period runs from
    the earliest line to the latest, control ``none``."""
    rows = [row.strip() for row in text.splitlines() if row.strip()]
    if not rows or rows[0] != FX_LINES:
        raise NotRecognised
    currency = "CAD"
    identifier = FX_IDENTIFIER
    found: list[tuple[dt.date, Decimal, Decimal | None, str | None, str]] = []
    for row in rows[1:]:
        head, _, rest = row.partition(" ")
        if head == "CURRENCY":
            currency = rest
        elif head == "ACCOUNT":
            identifier = rest
        elif head == "LINE":
            posting, amount, stated, original, description = rest.split(" ", 4)
            found.append(
                (
                    dt.date.fromisoformat(posting),
                    Decimal(amount),
                    None if stated == "-" else Decimal(stated),
                    None if original == "-" else original,
                    description,
                )
            )
    first, last = min(line[0] for line in found), max(line[0] for line in found)
    matched = accounts.by_identifier(FX_INSTITUTION, identifier, first, last)
    if len(matched) != 1:
        raise Rejected("unknown_account", (first, last))
    account = matched[0]
    period = ParsedPeriod(
        account=account,
        currency=currency,
        period_start=first,
        period_end=last,
        control_kind="none",
    )
    return ParseResult(
        periods=(period,),
        lines=tuple(
            ParsedLine(
                line_kind="transaction",
                account=account,
                posting_date=posting,
                amount=amount,
                currency=currency,
                stated_cad=stated,
                original_currency=original,
                original_amount=None if original is None else amount,
                description_raw=description,
            )
            for posting, amount, stated, original, description in found
        ),
        control_kind="none",
    )


FX_PARSER = Parser("synthetic_fx_txt", FX_ISSUER, "txt", "1", parse_fx)


def fx_lines(currency: str, *lines: str, account: str = FX_IDENTIFIER) -> str:
    """An ``FX_LINES`` document of the account ``account`` identifies; each line is
    ``"<date> <amount> <stated|-> <original|-> <description>"``."""
    return "\n".join(
        [FX_LINES, f"CURRENCY {currency}", f"ACCOUNT {account}",
         *(f"LINE {x}" for x in lines)]
    )  # fmt: skip


def read_as_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def csv_row(day: str, description: str, *, out: str = "", into: str = "",
            balance: str) -> str:  # fmt: skip
    """One headerless Vancity chequing row of the fixtures' fake account."""
    return f'{CSV_IDENTIFIER},{day},"{description}",,{out},{into},{balance}'


# The pairing parameters of the design spec's worked examples (a 5.00 CAD floor, a
# 7-day transfer window, a 5 percent conversion spread), so the rule tests read as the
# spec does. The shipped defaults are tuned (settings.py, ADR-0012) and pinned by
# ``test_tuned_defaults``, which drops these.
RULE_SETTINGS = (
    "[finance.transfers]\nwindow_days = 7\nfee_tolerance_abs = 5.00\n"
    "[finance.conversions]\nspread_tolerance_pct = 5.0\n"
)


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
                f'downloads = "{tmp_path / "downloads-unset"}"\n' + RULE_SETTINGS
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

    def card(self, slug: str = "card") -> None:
        """The one account the card fixtures name."""
        self.accounts((slug, CARD_INSTITUTION, "credit_card", CARD_IDENTIFIER))

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
