"""The parser registry and ``parse_document``, derivation stage 1 for one document
(FR-3, FR-9).

Parsers register under an id with the issuer directory and file format they read
and a version (``register_parser``); ``PARSER_MODULES`` lists every module that
registers one, and ``load_parsers`` imports them. ``parse_document`` picks the
parsers for the document's issuer and format, gives each the document's text in id
order, and keeps the first result that is not ``NotRecognised``. Then:

- no parser for the issuer and format, or none recognises the text: ``unparsed``,
  zero lines (FR-9), with the reason in ``reject_reason``;
- a PDF with no text layer: ``needs_manual_entry`` (no OCR in v1);
- the parser rejects (an unregistered account gives ``unknown_account``, a failed
  check its own reason), or a ``summary`` period whose lines do not reconcile to the
  cent (``summary_mismatch``): ``rejected``, zero lines, one
  ``finance.document.rejected`` event, and a ``rejected_period`` row for each
  registered account the rejection names, with the period it states there (coverage
  reads it);
- otherwise ``parsed``: its ``statement_period`` and ``document_line`` rows are
  written, and ``tied_out`` is true on every summary period.

A document whose file is not where it is registered gives ``unparsed`` and a reason
instead of an error: its path is withheld (``WITHHELD_PATH``, which no parser
opens), the file has left the archive, or the bytes there are no longer the
document's (the sha256 differs). Reparsing a document first deletes its lines,
periods, rejected periods and rejection event, so the result depends only on the
bytes, the parsers and the account registry. Images and drop records are never parsed.

Nothing here prints or raises a document's content: outcomes are a status, a reason
code and counts.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import importlib
import uuid
from collections import defaultdict
from collections.abc import Callable, Mapping
from dataclasses import dataclass, fields
from decimal import Decimal
from pathlib import Path
from types import MappingProxyType
from typing import Annotated, Final

from pydantic import BaseModel, ConfigDict
from sqlalchemy import delete, insert, select, update

from rheo_finance.context import UnitOfWork
from rheo_finance.contracts import Tiered
from rheo_finance.documents.registry import WITHHELD_PATH, DocumentStatus
from rheo_finance.events import record_event
from rheo_finance.parsers.base import (
    AccountLookup,
    ManualReason,
    NoTextLayer,
    NotRecognised,
    Parse,
    ParsedLine,
    ParseResult,
    RegisteredAccount,
    RegisteredIdentifier,
    Rejected,
    RejectReason,
    UnparsedReason,
)
from rheo_finance.parsers.docextract_adapter import pdf_text
from rheo_finance.storage.ids import derived_id
from rheo_finance.storage.refs import format_ref
from rheo_finance.storage.schema import (
    account,
    account_identifier,
    document,
    document_line,
    event,
    rejected_period,
    statement_period,
)
from rheo_finance.tier import Tier

P = Tier.PUBLIC

# --- events ---------------------------------------------------------------------------

DOCUMENT_REJECTED: Final = "finance.document.rejected"


class DocumentRejected(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    document_ref: Annotated[str, Tiered(P)]
    reason: Annotated[RejectReason, Tiered(P)]


EVENT_MODELS: Final[Mapping[str, type[BaseModel]]] = MappingProxyType(
    {DOCUMENT_REJECTED: DocumentRejected}
)

# --- the registry ---------------------------------------------------------------------

# Every module that registers a parser at import.
PARSER_MODULES: Final[tuple[str, ...]] = (
    "rheo_finance.parsers.vancity_statement",
    "rheo_finance.parsers.vancity_chequing_csv",
    "rheo_finance.parsers.virgin_bill",
)


@dataclass(frozen=True)
class Parser:
    """A fixed-rule parser for files of ``format`` under issuer directory ``issuer``.
    ``parser_id`` and ``version`` are stored on every document it parses.

    ``filing_date``, when set, reads from the same text the date a new file is filed
    under (``documents.filer``): a statement's period end or a bill's date. It needs no
    account registry and raises ``NotRecognised`` where ``parse`` would.

    ``sets_hints`` says the parser sets its lines' hint columns from
    ``reference.patterns.classify``, so its output depends on ``patterns.json`` too."""

    parser_id: str
    issuer: str
    format: str
    version: str
    parse: Parse
    filing_date: Callable[[str], dt.date] | None = None
    sets_hints: bool = False


PARSERS: dict[str, Parser] = {}


def register_parser(parser: Parser) -> Parser:
    """Add ``parser``. A second parser with the same id, or with the same issuer,
    format and version (the triple FR-3 registers by), is an error."""
    if parser.parser_id in PARSERS:
        raise ValueError(f"parser {parser.parser_id} is already registered")
    triple = (parser.issuer, parser.format, parser.version)
    for other in PARSERS.values():
        if (other.issuer, other.format, other.version) == triple:
            raise ValueError(
                f"{parser.issuer}, {parser.format}, version {parser.version} is"
                f" already registered as {other.parser_id}"
            )
    PARSERS[parser.parser_id] = parser
    return parser


def load_parsers() -> dict[str, Parser]:
    for module in PARSER_MODULES:
        importlib.import_module(module)
    return PARSERS


def parsers_for(issuer: str, fmt: str) -> list[Parser]:
    return sorted(
        (each for each in load_parsers().values()
         if each.issuer == issuer and each.format == fmt),
        key=lambda each: each.parser_id,
    )  # fmt: skip


def account_lookup(uow: UnitOfWork) -> AccountLookup:
    """The loaded account registry (stage 2's ``account`` and ``account_identifier``),
    without the built-in account's ``internal`` identifier."""
    rows = uow.execute(
        select(
            account.c.id,
            account.c.slug,
            account.c.institution,
            account_identifier.c.identifier,
            account_identifier.c.kind,
            account_identifier.c.valid_from,
            account_identifier.c.valid_to,
        )
        .join(account, account.c.id == account_identifier.c.account_id)
        .where(account_identifier.c.kind != "internal")
    )
    return AccountLookup(
        RegisteredIdentifier(
            account=RegisteredAccount(row.id, row.slug),
            institution=row.institution,
            identifier=row.identifier,
            kind=row.kind,
            valid_from=row.valid_from,
            valid_to=row.valid_to,
        )
        for row in rows
    )


# --- the summary check ----------------------------------------------------------------


def summary_ties_out(result: ParseResult) -> bool:
    """Whether every ``summary`` period reconciles to the cent with its own lines,
    and every money line belongs to a period.

    A period's lines are its account's and currency's lines that carry an amount:
    opening plus their sum is closing, and where the period states them, the debits
    (negative lines, as a positive total) and credits (positive lines) equal the
    stated totals. A summary period without an opening or a closing does not tie out.
    """
    sums: dict[tuple[uuid.UUID, str], list[Decimal]] = defaultdict(list)
    for line in result.lines:
        if line.amount is None:
            continue
        if line.account is None or line.currency is None:
            return False
        sums[line.account.id, line.currency].append(line.amount)
    keys = {(period.account.id, period.currency) for period in result.periods}
    if not set(sums) <= keys:
        return False
    for period in result.periods:
        if period.control_kind != "summary":
            continue
        amounts = sums.get((period.account.id, period.currency), [])
        if period.opening is None or period.closing is None:
            return False
        if period.opening + sum(amounts, Decimal(0)) != period.closing:
            return False
        debits = sum((-each for each in amounts if each < 0), Decimal(0))
        credits = sum((each for each in amounts if each > 0), Decimal(0))
        if period.total_debits is not None and debits != period.total_debits:
            return False
        if period.total_credits is not None and credits != period.total_credits:
            return False
    return True


# --- parse_document -------------------------------------------------------------------


@dataclass(frozen=True)
class Parsed:
    """What happened to one document: counts and codes only. ``segments`` and
    ``gaps`` count a running-balance document's verified segments and breaks."""

    status: DocumentStatus
    reason: RejectReason | UnparsedReason | ManualReason | None
    periods: int = 0
    lines: int = 0
    segments: int = 0
    gaps: int = 0


def _clear(uow: UnitOfWork, document_id: uuid.UUID, ref: str) -> None:
    uow.execute(delete(document_line).where(document_line.c.document_id == document_id))
    uow.execute(
        delete(statement_period).where(statement_period.c.document_id == document_id)
    )
    uow.execute(
        delete(rejected_period).where(rejected_period.c.document_id == document_id)
    )
    uow.execute(
        delete(event).where(
            event.c.type == DOCUMENT_REJECTED, event.c.subject_ref == ref
        )
    )


def _set_status(
    uow: UnitOfWork,
    document_id: uuid.UUID,
    parsed: Parsed,
    *,
    parser: Parser | None = None,
    period: tuple[dt.date, dt.date] | None = None,
    official: bool | None = None,
) -> Parsed:
    values: dict[str, object] = {
        "status": parsed.status,
        "reject_reason": parsed.reason,
        "parser_id": parser.parser_id if parser else None,
        "parser_version": parser.version if parser else None,
        "extraction": "fixed_rule" if parsed.status == "parsed" else "none",
        "period_start": period[0] if period else None,
        "period_end": period[1] if period else None,
    }
    if official is not None:
        values["official"] = official
    uow.execute(update(document).where(document.c.id == document_id).values(**values))
    return parsed


def _line_values(
    document_id: uuid.UUID, line_no: int, line: ParsedLine, fallback: dt.date
) -> dict[str, object]:
    values = {
        each.name: getattr(line, each.name)
        for each in fields(line)
        if each.name != "account"
    }
    values["account_id"] = line.account.id if line.account else None
    business_date = line.posting_date or (
        line.occurred_at.astimezone(dt.UTC).date() if line.occurred_at else fallback
    )
    values["id"] = derived_id(business_date, ("document_line", document_id, line_no))
    values["document_id"] = document_id
    values["line_no"] = line_no
    return values


def _period_values(
    document_id: uuid.UUID, result: ParseResult
) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for period in result.periods:
        values = {
            each.name: getattr(period, each.name)
            for each in fields(period)
            if each.name not in ("account", "overdrawn")
        }
        values["document_id"] = document_id
        values["account_id"] = period.account.id
        if period.control_kind == "summary":
            values["tied_out"] = True  # summary_ties_out held for the whole document
        rows.append(values)
    return rows


def _read_text(
    path: Path, data: bytes, fmt: str, extract_pdf: Callable[[Path], str]
) -> str | None:
    """The document's text, or None when a non-PDF file is not UTF-8 text.

    ``data`` is the verified bytes; a PDF is read again by pdftotext at ``path``.
    """
    if fmt == "pdf":
        return extract_pdf(path)
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return None


def parse_document(
    uow: UnitOfWork,
    document_id: uuid.UUID,
    archive: Path,
    *,
    extract_pdf: Callable[[Path], str] = pdf_text,
) -> Parsed:
    """Parse one registered document from the archive at ``archive`` and store the
    outcome. ``extract_pdf`` turns a PDF into its layout text (``pdf_text``)."""
    row = uow.execute(
        select(
            document.c.path,
            document.c.sha256,
            document.c.kind,
            document.c.issuer,
            document.c.format,
            document.c.status,
            document.c.imported_at,
        ).where(document.c.id == document_id)
    ).one_or_none()
    if row is None:
        raise LookupError("no document has this id")
    ref = format_ref("finance", "document", document_id)
    _clear(uow, document_id, ref)
    if row.kind in ("image", "drop_record"):
        return Parsed(row.status, None)

    candidates = parsers_for(row.issuer, row.format)
    if not candidates:
        return _set_status(uow, document_id, Parsed("unparsed", "no_parser"))
    if row.path == WITHHELD_PATH:
        return _set_status(uow, document_id, Parsed("unparsed", "path_withheld"))
    path = archive / row.path
    try:
        data = path.read_bytes()
    except OSError:
        return _set_status(uow, document_id, Parsed("unparsed", "file_missing"))
    if hashlib.sha256(data).hexdigest() != row.sha256:
        return _set_status(uow, document_id, Parsed("unparsed", "file_changed"))
    try:
        text = _read_text(path, data, row.format, extract_pdf)
    except NoTextLayer:
        return _set_status(
            uow, document_id, Parsed("needs_manual_entry", "no_text_layer")
        )
    if text is None:
        return _set_status(uow, document_id, Parsed("unparsed", "not_text"))

    lookup = account_lookup(uow)
    for parser in candidates:
        try:
            result = parser.parse(text, lookup)
        except NotRecognised:
            continue
        except Rejected as rejected:
            return _reject(uow, document_id, ref, row.imported_at, parser, rejected)
        if not summary_ties_out(result):
            mismatch = Rejected("summary_mismatch", _span(result))
            return _reject(
                uow,
                document_id,
                ref,
                row.imported_at,
                parser,
                mismatch,
                _account_spans(result),
            )
        return _store(uow, document_id, parser, result)
    return _set_status(uow, document_id, Parsed("unparsed", "not_recognised"))


def _span(result: ParseResult) -> tuple[dt.date, dt.date] | None:
    if not result.periods:
        return None
    return (
        min(period.period_start for period in result.periods),
        max(period.period_end for period in result.periods),
    )


Span = tuple[uuid.UUID, dt.date, dt.date]


def _account_spans(result: ParseResult) -> list[Span]:
    """Each account's span over the periods a result states for it."""
    found: dict[uuid.UUID, tuple[dt.date, dt.date]] = {}
    for period in result.periods:
        start, end = found.get(
            period.account.id, (period.period_start, period.period_end)
        )
        found[period.account.id] = (
            min(start, period.period_start),
            max(end, period.period_end),
        )
    return [(account_id, *span) for account_id, span in found.items()]


def _reject(
    uow: UnitOfWork,
    document_id: uuid.UUID,
    ref: str,
    imported_at: dt.datetime,
    parser: Parser,
    rejected: Rejected,
    spans: list[Span] | None = None,
) -> Parsed:
    """Store a rejection. ``spans`` are the accounts and periods it names; by default
    every account the parser named, over the period it stated."""
    parsed = _set_status(
        uow,
        document_id,
        Parsed("rejected", rejected.reason),
        parser=parser,
        period=rejected.period,
    )
    if spans is None:
        period = rejected.period
        spans = (
            [(each.id, *period) for each in dict.fromkeys(rejected.accounts)]
            if period
            else []
        )
    if spans:
        uow.execute(
            insert(rejected_period),
            [
                {
                    "document_id": document_id,
                    "account_id": account_id,
                    "period_start": start,
                    "period_end": end,
                }
                for account_id, start, end in spans
            ],
        )
    # Keyed on the document alone and dated by its registration, so a reparse or a
    # rebuild recreates the same event.
    record_event(
        uow,
        EVENT_MODELS,
        DOCUMENT_REJECTED,
        ref,
        imported_at.astimezone(dt.UTC).date(),
        DocumentRejected(document_ref=ref, reason=rejected.reason),
    )
    return parsed


def _store(
    uow: UnitOfWork, document_id: uuid.UUID, parser: Parser, result: ParseResult
) -> Parsed:
    span = _span(result)
    fallback = span[0] if span else dt.date(2000, 1, 1)
    if result.periods:
        uow.execute(insert(statement_period), _period_values(document_id, result))
    if result.lines:
        uow.execute(
            insert(document_line),
            [
                _line_values(document_id, number, line, fallback)
                for number, line in enumerate(result.lines, start=1)
            ],
        )
    return _set_status(
        uow,
        document_id,
        Parsed(
            "parsed",
            None,
            periods=len(result.periods),
            lines=len(result.lines),
            segments=len(result.segments),
            gaps=len(result.gaps),
        ),
        parser=parser,
        period=span,
        official=result.official,
    )
