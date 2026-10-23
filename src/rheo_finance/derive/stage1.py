"""Derivation stage 1: parse every registered document (FR-3, FR-9).

Each document goes through ``parsers.registry.parse_document``, which deletes the
document's own lines, periods and rejection event first and then stores the outcome, so
the result depends only on the bytes, the parsers and the account registry. Derive
clears the parse cache before stage 2 rewrites ``account``, so a document whose
account has left the registry is parsed again against the new registry (and is
rejected ``unknown_account`` if nothing matches), not carried with a stale account key.

After every document is parsed, ``precedence.supersede`` marks ``superseded`` each
parsed document that an equally ranked one with a larger sha256 repeats for exactly
the same periods, so stage 3 reads one copy.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable
from pathlib import Path

from sqlalchemy import select

from rheo_finance.context import UnitOfWork
from rheo_finance.derive.precedence import supersede
from rheo_finance.parsers.docextract_adapter import pdf_text
from rheo_finance.parsers.registry import parse_document
from rheo_finance.storage.schema import document


def parse_all(
    uow: UnitOfWork,
    archive: Path,
    *,
    extract_pdf: Callable[[Path], str] = pdf_text,
) -> Counter[str]:
    """Parse every registered document, then supersede repeated copies; the count of
    documents by resulting status."""
    outcomes: Counter[str] = Counter()
    for document_id in uow.scalars(select(document.c.id).order_by(document.c.id)).all():
        outcomes[
            parse_document(uow, document_id, archive, extract_pdf=extract_pdf).status
        ] += 1
    superseded = supersede(uow)
    if superseded:
        outcomes["parsed"] -= superseded
        outcomes["superseded"] += superseded
    return +outcomes
