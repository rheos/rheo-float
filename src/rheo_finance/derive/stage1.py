"""Derivation stage 1: parse the registered documents (FR-3, FR-5, FR-9).

Each document parsed goes through ``parsers.registry.parse_document``, which deletes
the document's own lines, periods and rejection event first and then stores the
outcome, so the result depends only on the bytes, the parsers and the account
registry.

A parse is kept rather than redone when none of those three moved. ``parse_inputs``
hashes the last two for one document: its issuer and format, the id and version of
every parser registered for them, and the account registry (``account`` joined to
its identifiers, as the parsers see it). Each parse stores that hash on the document.
Without ``reparse`` (``finance.ledger.derive``), a document is parsed again only when
it was never parsed, its stored hash differs from the current one, its file is not at
its path with its own sha256 (the file left the archive, was withheld or now holds
other bytes), or its last outcome was about the file rather than its content (one of
``FILE_REASONS``: the file is back now); otherwise its stored parse output stands.
With ``reparse`` (``finance.ledger.rebuild``) every document is parsed again.

The account registry enters the hash whole: an account that leaves or joins, or any
identifier change, parses every document again. shortcut: broader than needed only
in cost, never in result; scope it to the parser's institution if registry edits ever
make derives slow.

After parsing, ``precedence.supersede`` marks ``superseded`` each parsed document
that an equally ranked one with a larger sha256 repeats for exactly the same periods,
so stage 3 reads one copy. Superseding is decided again on every run over the whole
set, so a kept document marked ``superseded`` last time starts this run ``parsed``.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from sqlalchemy import select, update

from rheo_finance.context import UnitOfWork
from rheo_finance.derive.precedence import supersede
from rheo_finance.documents.registry import WITHHELD_PATH
from rheo_finance.parsers.docextract_adapter import pdf_text
from rheo_finance.parsers.registry import parse_document, parsers_for
from rheo_finance.storage.schema import account, account_identifier, document

# Outcomes that say where the file was, not what it holds: never kept.
FILE_REASONS: Final = frozenset({"file_missing", "file_changed", "path_withheld"})


@dataclass(frozen=True)
class Parsing:
    """What stage 1 did: documents by resulting status, and how many were parsed now
    and how many kept their stored parse output."""

    outcomes: Counter[str]
    parsed_now: int
    kept: int


def registry_digest(uow: UnitOfWork) -> str:
    """sha256 of the account registry the parsers match against."""
    rows = uow.execute(
        select(
            account.c.id,
            account.c.slug,
            account.c.institution,
            account_identifier.c.identifier,
            account_identifier.c.kind,
            account_identifier.c.valid_from,
            account_identifier.c.valid_to,
        ).join(account, account.c.id == account_identifier.c.account_id)
    )
    body = sorted([None if each is None else str(each) for each in row] for row in rows)
    return hashlib.sha256(json.dumps(body).encode("utf-8")).hexdigest()


def parse_inputs(issuer: str, fmt: str, registry: str) -> str:
    """sha256 of what one document's parse depends on besides its bytes."""
    parsers = [[each.parser_id, each.version] for each in parsers_for(issuer, fmt)]
    body = json.dumps([issuer, fmt, parsers, registry])
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _same_bytes(archive: Path, relative: str, sha256: str) -> bool:
    """Whether the archive still holds the document's bytes at its path."""
    if relative == WITHHELD_PATH:
        return False
    try:
        with (archive / relative).open("rb") as handle:
            return hashlib.file_digest(handle, "sha256").hexdigest() == sha256
    except OSError:
        return False


def parse_all(
    uow: UnitOfWork,
    archive: Path,
    *,
    extract_pdf: Callable[[Path], str] = pdf_text,
    reparse: bool = False,
) -> Parsing:
    """Parse every registered document that needs it (every one with ``reparse``),
    then supersede repeated copies."""
    registry = registry_digest(uow)
    uow.execute(
        update(document)
        .where(document.c.status == "superseded")
        .values(status="parsed")
    )
    outcomes: Counter[str] = Counter()
    parsed_now = kept = 0
    rows = uow.execute(
        select(
            document.c.id,
            document.c.path,
            document.c.sha256,
            document.c.issuer,
            document.c.format,
            document.c.status,
            document.c.reject_reason,
            document.c.parse_inputs,
        ).order_by(document.c.id)
    ).all()
    for row in rows:
        inputs = parse_inputs(row.issuer, row.format, registry)
        if (
            not reparse
            and row.status != "filed"
            and row.parse_inputs == inputs
            and row.reject_reason not in FILE_REASONS
            and _same_bytes(archive, row.path, row.sha256)
        ):
            outcomes[row.status] += 1
            kept += 1
            continue
        parsed = parse_document(uow, row.id, archive, extract_pdf=extract_pdf)
        uow.execute(
            update(document).where(document.c.id == row.id).values(parse_inputs=inputs)
        )
        outcomes[parsed.status] += 1
        parsed_now += 1
    superseded = supersede(uow)
    if superseded:
        outcomes["parsed"] -= superseded
        outcomes["superseded"] += superseded
    return Parsing(outcomes=+outcomes, parsed_now=parsed_now, kept=kept)
