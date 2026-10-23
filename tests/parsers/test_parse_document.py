"""``parse_document`` and the parser registry beyond one parser (FR-3, FR-9).

A fixture parser registered for issuer ``mbna`` (no real parser reads it yet) drives
the registry's own summary check; the other tests cover a document whose file is not
where it was registered, a PDF with no text layer, and the registry rules. Every value
is synthetic.
"""

from __future__ import annotations

import ast
import datetime as dt
import hashlib
import uuid
from collections.abc import Callable
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import BaseModel
from sqlalchemy import Connection, func, select, update

from rheo_finance.context import UnitOfWork
from rheo_finance.documents.registry import WITHHELD_PATH, document_id, file_archive
from rheo_finance.events import record_event
from rheo_finance.manual.accounts import full_number
from rheo_finance.parsers import registry
from rheo_finance.parsers.base import (
    AccountLookup,
    NoTextLayer,
    NotRecognised,
    ParsedLine,
    ParsedPeriod,
    ParseResult,
    RegisteredAccount,
    RegisteredIdentifier,
)
from rheo_finance.parsers.docextract_adapter import pdf_text
from rheo_finance.parsers.registry import (
    DOCUMENT_REJECTED,
    EVENT_MODELS,
    PARSER_MODULES,
    DocumentRejected,
    Parsed,
    Parser,
    parse_document,
)
from rheo_finance.storage.schema import document, document_line, event, statement_period

from .conftest import ManualAccount, register_accounts

NOW = dt.datetime(2026, 1, 15, 12, 0, tzinfo=dt.UTC)
PACKAGE = Path(registry.__file__).resolve().parent


CARD = ManualAccount(
    "card", "Card Bank", "credit_card", ("9999 99** **** 4321",), "masked_card"
)


def fixture_parser(text: str, accounts: AccountLookup) -> ParseResult:
    """Reads ``CARD <opening> <closing>`` then one ``<amount>`` per line."""
    head, *rows = text.split("\n")
    if not head.startswith("CARD "):
        raise NotRecognised("not the fixture layout")
    _, opening, closing = head.split()
    start, end = dt.date(2026, 1, 1), dt.date(2026, 1, 31)
    (card,) = accounts.by_last_four("card bank", "4321", start, end)
    amounts = [Decimal(row) for row in rows if row.strip()]
    return ParseResult(
        periods=(
            ParsedPeriod(
                card,
                "CAD",
                start,
                end,
                "summary",
                opening=Decimal(opening),
                closing=Decimal(closing),
            ),
        ),  # fmt: skip
        lines=tuple(
            ParsedLine(
                "transaction", card, posting_date=start, amount=a, currency="CAD"
            )
            for a in amounts
        ),
        control_kind="summary",
    )


@pytest.fixture
def fixture_registry(monkeypatch: pytest.MonkeyPatch) -> dict[str, Parser]:
    parsers = dict(registry.load_parsers())
    monkeypatch.setattr(registry, "PARSERS", parsers)
    registry.register_parser(Parser("mbna.fixture", "mbna", "pdf", "7", fixture_parser))
    return parsers


def read_as_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


class Archive:
    def __init__(self, conn: Connection, tmp_path: Path) -> None:
        self.conn = conn
        self.uow = UnitOfWork(conn)
        self.data = tmp_path / "data"
        self.root = tmp_path / "archive"
        self.root.mkdir()
        register_accounts(self.uow, self.data, CARD)

    def file(self, relative: str, data: bytes) -> uuid.UUID:
        target = self.root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        file_archive(self.uow, self.root, self.data, NOW)
        return document_id(hashlib.sha256(data).hexdigest())

    def parse(
        self, doc: uuid.UUID, extract_pdf: Callable[[Path], str] = read_as_text
    ) -> Parsed:
        return parse_document(self.uow, doc, self.root, extract_pdf=extract_pdf)

    def lines(self, doc: uuid.UUID) -> int:
        query = select(func.count()).select_from(document_line)
        return int(
            self.conn.scalar(query.where(document_line.c.document_id == doc)) or 0
        )

    def events(self) -> int:
        query = select(func.count()).select_from(event)
        return int(
            self.conn.scalar(query.where(event.c.type == DOCUMENT_REJECTED)) or 0
        )


@pytest.fixture
def archive(owner_conn: Connection, tmp_path: Path) -> Archive:
    return Archive(owner_conn, tmp_path)


def test_lines_that_reconcile_to_the_summary_commit(
    archive: Archive, fixture_registry: dict[str, Parser]
) -> None:
    doc = archive.file("mbna/2026-01.pdf", b"CARD 100.00 75.50\n-30.00\n5.50\n")
    assert archive.parse(doc) == Parsed("parsed", None, periods=1, lines=2)
    tied = archive.conn.scalar(
        select(statement_period.c.tied_out).where(statement_period.c.document_id == doc)
    )
    assert tied is True
    parser = archive.conn.execute(
        select(document.c.parser_id, document.c.parser_version).where(
            document.c.id == doc
        )
    ).one()
    assert tuple(parser) == ("mbna.fixture", "7")


def test_lines_one_cent_off_the_summary_are_rejected(
    archive: Archive, fixture_registry: dict[str, Parser]
) -> None:
    doc = archive.file("mbna/2026-01.pdf", b"CARD 100.00 75.51\n-30.00\n5.50\n")
    assert archive.parse(doc) == Parsed("rejected", "summary_mismatch")
    assert archive.lines(doc) == 0
    assert archive.events() == 1
    period = archive.conn.execute(
        select(document.c.period_start, document.c.period_end).where(
            document.c.id == doc
        )
    ).one()
    assert tuple(period) == (dt.date(2026, 1, 1), dt.date(2026, 1, 31))


def test_stated_totals_must_match_the_lines_too() -> None:
    card = RegisteredAccount(uuid.uuid4(), "card")
    start = dt.date(2026, 1, 1)
    lines = tuple(
        ParsedLine("transaction", card, posting_date=start, amount=a, currency="CAD")
        for a in (Decimal("-30.00"), Decimal("5.50"))
    )

    def result(debits: str, credits: str) -> ParseResult:
        period = ParsedPeriod(
            card, "CAD", start, start, "summary",
            opening=Decimal("100.00"), closing=Decimal("75.50"),
            total_debits=Decimal(debits), total_credits=Decimal(credits),
        )  # fmt: skip
        return ParseResult((period,), lines, "summary")

    assert registry.summary_ties_out(result("30.00", "5.50"))
    assert not registry.summary_ties_out(result("30.01", "5.50"))
    assert not registry.summary_ties_out(result("30.00", "5.49"))
    stray = ParseResult(result("30.00", "5.50").periods, (*lines, lines[0]), "summary")
    assert not registry.summary_ties_out(stray), "the extra line breaks the closing"


def test_a_document_with_no_parser_for_its_issuer_is_unparsed(archive: Archive) -> None:
    doc = archive.file("cra/2026-01.pdf", b"anything at all\n")
    assert archive.parse(doc) == Parsed("unparsed", "no_parser")
    assert archive.lines(doc) == 0


def test_a_file_gone_from_the_archive_is_unparsed_with_a_reason(
    archive: Archive, fixture_registry: dict[str, Parser]
) -> None:
    doc = archive.file("mbna/2026-01.pdf", b"CARD 100.00 75.50\n-30.00\n5.50\n")
    (archive.root / "mbna" / "2026-01.pdf").unlink()
    assert archive.parse(doc) == Parsed("unparsed", "file_missing")


def test_changed_bytes_at_the_path_are_not_parsed_as_the_document(
    archive: Archive, fixture_registry: dict[str, Parser]
) -> None:
    doc = archive.file("mbna/2026-01.pdf", b"CARD 100.00 75.50\n-30.00\n5.50\n")
    (archive.root / "mbna" / "2026-01.pdf").write_bytes(b"CARD 1.00 1.00\n")
    assert archive.parse(doc) == Parsed("unparsed", "file_changed")
    assert archive.lines(doc) == 0


def test_a_withheld_path_is_never_opened(
    archive: Archive, fixture_registry: dict[str, Parser]
) -> None:
    doc = archive.file("mbna/2026-01.pdf", b"CARD 100.00 75.50\n-30.00\n5.50\n")
    archive.conn.execute(
        update(document).where(document.c.id == doc).values(path=WITHHELD_PATH)
    )

    def refuse(_path: Path) -> str:
        raise AssertionError("a withheld path was opened")

    assert archive.parse(doc, extract_pdf=refuse) == Parsed("unparsed", "path_withheld")


def test_a_pdf_with_no_text_layer_goes_to_manual_entry(
    archive: Archive, fixture_registry: dict[str, Parser]
) -> None:
    doc = archive.file("mbna/2026-01.pdf", b"scanned\n")

    def scan(_path: Path) -> str:
        raise NoTextLayer("no text")

    assert archive.parse(doc, extract_pdf=scan) == Parsed(
        "needs_manual_entry", "no_text_layer"
    )


def test_pdf_text_reports_a_file_with_no_text_layer(tmp_path: Path) -> None:
    """The adapter's real pdftotext call on a file that is not a PDF."""
    path = tmp_path / "not-a-pdf.pdf"
    path.write_bytes(b"plain bytes\n")
    with pytest.raises(NoTextLayer):
        pdf_text(path)


def test_an_image_is_left_for_manual_entry(archive: Archive) -> None:
    doc = archive.file("mbna/photo.jpg", b"pretend pixels\n")
    assert archive.parse(doc) == Parsed("needs_manual_entry", None)


def test_a_parser_id_registers_once(fixture_registry: dict[str, Parser]) -> None:
    with pytest.raises(ValueError, match="already registered"):
        registry.register_parser(
            Parser("mbna.fixture", "mbna", "csv", "1", fixture_parser)
        )


def test_an_issuer_format_and_version_register_once(
    fixture_registry: dict[str, Parser],
) -> None:
    with pytest.raises(ValueError, match="already registered as mbna.fixture"):
        registry.register_parser(
            Parser("mbna.other", "mbna", "pdf", "7", fixture_parser)
        )
    registry.register_parser(Parser("mbna.other", "mbna", "pdf", "8", fixture_parser))
    assert "mbna.other" in fixture_registry


def test_every_module_that_registers_a_parser_is_listed() -> None:
    registering = set()
    for path in sorted(PACKAGE.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "register_parser"
            ):
                registering.add(f"rheo_finance.parsers.{path.stem}")
    assert registering == set(PARSER_MODULES)
    assert "vancity.statement" in registry.load_parsers()


def test_an_event_needs_its_registered_model(owner_conn: Connection) -> None:
    uow = UnitOfWork(owner_conn)
    data = DocumentRejected(
        document_ref="finance.document:01890000-0000-7000-8000-000000000000",
        reason="unknown_account",
    )
    with pytest.raises(ValueError, match="not a registered event type"):
        record_event(
            uow, EVENT_MODELS, "finance.thing.happened", "x.y:z", NOW.date(), data
        )
    with pytest.raises(TypeError):
        record_event(
            uow,
            {DOCUMENT_REJECTED: _Other},
            DOCUMENT_REJECTED,
            "x.y:z",
            NOW.date(),
            data,
        )


class _Other(BaseModel):
    pass


@pytest.mark.parametrize(
    ("registered", "printed"),
    [
        ("SAMPLE99901A9ID", "sample-99901-a9id"),
        ("999 000 123", "999000123"),
        ("999/000/123", "999-000-123"),
        ("999000123", "999000124"),
        ("SAMPLE99901A9ID", "SAMPLE99901A9IE"),
    ],
)
def test_the_loader_and_the_lookup_agree_on_one_number(
    registered: str, printed: str
) -> None:
    """The loader's "same full number" and ``by_identifier``'s whole-number match
    use one normalizer: a number the loader calls the same is the one the lookup
    finds."""
    holder = RegisteredAccount(uuid.uuid4(), "holder")
    lookup = AccountLookup(
        [RegisteredIdentifier(holder, "Sample Bank", registered, "number")]
    )
    day = dt.date(2026, 1, 1)
    same = full_number("number", registered) == full_number("number", printed)
    assert full_number("number", registered) is not None

    assert (lookup.by_identifier("sample bank", printed, day, day) == (holder,)) is same
