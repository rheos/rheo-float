"""The input fingerprint (SPEC "Derivation"): one sha256 over everything a derive reads
that can change between two runs.

Its parts:

- ``archive``: every file under ``finance.paths.archive`` with its size and mtime
  (``absent`` when the directory does not exist);
- ``manual``: the sha256 of each manual file (``absent`` for a missing one);
- ``decisions``: the newest decision's id and the count (a decision's items and
  targets are written with it and never change);
- ``rates``: the count of stored rates and the newest ``fetched_at``, so a fetch that
  stores a rate makes the next derive convert again rather than reuse its result;
- ``settings``: ``settings.settings_hash``;
- ``parsers``: every registered parser's id, issuer, format and version, the
  installed ``doc-extraction-api`` (``docextract_adapter.docextract_version``), and
  the digest of ``reference/patterns.json`` (``reference.patterns.patterns_digest``),
  which the hint-setting parsers read;
- ``features``: the feature packages in the tree (Key decision 14);
- ``local_date``: today in ``finance.locale.timezone``. Stage 6 reads today, and later
  stage 14's overdue figures and stage 16's overdue-statement event do too, so a run
  after local midnight differs from the last one before it even when nothing else
  moved; inside one local day it does not.

``STATE_PARTS`` names the part each input table feeds: every state table
(``run.STATE``) and the registry the file operation keeps (``run.FILE_OWNED``, which a
filing changes only by changing the archive), checked by a guard test. A new input
table (the stored model proposals, Phase 4) adds its part and its row there.

The tick's dirty check compares the fingerprint with the last successful run's (prompt
66); here ``finance.ledger.derive`` and ``finance.ledger.rebuild`` compute it before
they write and return it.

Paths and file names are hashed and never returned.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import pkgutil
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from sqlalchemy import func, select

from rheo_finance import features
from rheo_finance.clock import local_today
from rheo_finance.context import FinanceContext, UnitOfWork
from rheo_finance.documents.registry import ARCHIVE_KEY
from rheo_finance.manual.headers import MANUAL_DIR_NAME, MANUAL_FILES
from rheo_finance.parsers import docextract_adapter
from rheo_finance.parsers.registry import load_parsers
from rheo_finance.reference import patterns
from rheo_finance.settings import settings_hash
from rheo_finance.storage.schema import decision, rate

ABSENT = "absent"

# Each input table a derive reads and does not rebuild, and the part it feeds.
STATE_PARTS: Final[Mapping[str, str]] = {
    "decision": "decisions",
    "decision_item": "decisions",
    "decision_target": "decisions",
    "rate": "rates",
    "document": "archive",
    "archive_skip": "archive",
}


@dataclass(frozen=True)
class Fingerprint:
    digest: str
    local_date: dt.date
    # Each part's own sha256 (local_date as ISO), by part name.
    parts: Mapping[str, str]


def _sha(value: object) -> str:
    return hashlib.sha256(json.dumps(value).encode("utf-8")).hexdigest()


def _archive_listing(root: Path | None) -> str:
    if root is None or not root.is_dir():
        return ABSENT
    listing: list[tuple[str, int, int]] = []
    for current, dirs, names in os.walk(root, followlinks=False):
        dirs.sort()
        base = Path(current)
        for name in sorted(names):
            path = base / name
            try:
                stat = path.lstat()
            except OSError:
                continue
            listing.append(
                (path.relative_to(root).as_posix(), stat.st_size, stat.st_mtime_ns)
            )
    return _sha(sorted(listing))


def _manual(data_dir: Path) -> str:
    hashes: list[tuple[str, str]] = []
    for name in sorted(MANUAL_FILES):
        path = data_dir / MANUAL_DIR_NAME / name
        try:
            hashes.append((name, hashlib.sha256(path.read_bytes()).hexdigest()))
        except OSError:
            hashes.append((name, ABSENT))
    return _sha(hashes)


def _decisions(uow: UnitOfWork) -> str:
    newest = uow.scalar(
        select(decision.c.id)
        .order_by(decision.c.decided_at.desc(), decision.c.id.desc())
        .limit(1)
    )
    count = uow.scalar(select(func.count()).select_from(decision))
    return _sha([None if newest is None else str(newest), int(count or 0)])


def _rates(uow: UnitOfWork) -> str:
    count, newest = uow.execute(
        select(func.count(), func.max(rate.c.fetched_at)).select_from(rate)
    ).one()
    return _sha([int(count or 0), None if newest is None else newest.isoformat()])


def _parsers() -> str:
    registered = sorted(
        [each.parser_id, each.issuer, each.format, each.version]
        for each in load_parsers().values()
    )
    return _sha(
        [
            registered,
            docextract_adapter.docextract_version(),
            patterns.patterns_digest(),
        ]
    )


def _features() -> str:
    found = pkgutil.iter_modules(features.__path__)
    return _sha(sorted(module.name for module in found))


def fingerprint(uow: UnitOfWork, ctx: FinanceContext) -> Fingerprint:
    """The input fingerprint of a derive run now."""
    today = local_today(ctx.clock, ctx.settings.timezone)
    parts = {
        "archive": _archive_listing(ctx.settings.get_path(ARCHIVE_KEY)),
        "manual": _manual(ctx.paths.data),
        "decisions": _decisions(uow),
        "rates": _rates(uow),
        "settings": settings_hash(ctx.settings),
        "parsers": _parsers(),
        "features": _features(),
        "local_date": today.isoformat(),
    }
    return Fingerprint(
        digest=_sha(sorted(parts.items())), local_date=today, parts=parts
    )
