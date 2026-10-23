"""The archive walk and the document registry, and ``finance.document.file`` (FR-2).

The operation runs the Downloads filer (``documents.filer``) first and then the walk,
so a file it copies in is registered by the same run.

The identifiers the check refuses come from the manual identifiers file itself and
from ``account_identifier``, so the walk never depends on derivation stage 2 having
run first (a rebuild registers files before stage 2 loads the registry). The file is
read failing closed: one whose rows cannot all be trusted (a header that is not the
expected one, a row that is not valid CSV, a cell holding a line break) refuses the
whole walk, because an identifier the read lost would let a path carrying it be
stored.

The walk adopts the files under ``finance.paths.archive`` where they sit: it opens
each one for reading only, to hash it, and never moves, renames or writes one. Every
file it meets is skipped, registered or known, and every walk then rechecks the paths
already stored, withholding any it now refuses.

- **Skipped and counted.** The adopted-path check (``PathCheck``) runs on the
  archive-relative path before anything else: a path carrying a registered account
  identifier, or a run of 8 or more digits that is not a ``YYYYMMDD`` date, is not
  registered. Neither is a symlink or anything else that is not a regular file, a file
  outside every mapped directory (``issuers.ISSUER_DIRS``), an iCloud placeholder
  whose bytes are not on disk, or a file or directory that vanished or cannot be read
  mid-walk. Each lands in ``archive_skip`` as a count by issuer
  directory and reason, and its path is kept nowhere: not in a table, the output, an
  event or an error message.
- **Registered.** A ``document`` row keyed by the file's sha256, status ``filed``, or
  kind ``image`` and status ``needs_manual_entry`` for a photo (FR-9). A drop record is
  extraction ``transcription``; everything else ``none`` until a parser runs.
- **Known.** Bytes already registered are a no-op, so filing the same bytes twice
  leaves the count unchanged. When the same bytes sit at two paths, the first path in
  sorted order is the document's. A registered document whose bytes now sit only at
  another path follows them: its path and issuer are updated. A document whose bytes
  are gone from the archive keeps its row.
- **Withheld.** Every stored path is checked again on every walk. One the check now
  refuses (an identifier registered after the file was filed) is replaced by
  ``WITHHELD_PATH`` and counted in the output; the file is counted in ``archive_skip``
  like any other skipped file, and the document gets its path back once the owner
  renames the file.

Hidden files and directories (a leading ``.``, such as ``.DS_Store``) are not
documents and are passed over, except the iCloud placeholders counted above.

``archive_skip`` is rewritten on every walk; ``document`` only ever gains rows.
"""

from __future__ import annotations

import csv
import datetime as dt
import hashlib
import io
import os
import re
import uuid
from collections import Counter
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Annotated, Final, Literal

from pydantic import BaseModel, ConfigDict
from sqlalchemy import delete, insert, select, update

from rheo_finance.context import FinanceContext, UnitOfWork
from rheo_finance.contracts import Role, SafetyClass, Tiered
from rheo_finance.documents.issuers import Placement, place, skip_directory
from rheo_finance.manual.accounts import IDENTIFIERS_FILE, AccountIdentifierRow
from rheo_finance.manual.headers import MANUAL_DIR_NAME, MANUAL_FILES
from rheo_finance.manual.loader import INVALID_CSV, load_csv
from rheo_finance.operations import OperationError, OperationSpec, register
from rheo_finance.path_check import PathCheck
from rheo_finance.storage.ids import derived_id
from rheo_finance.storage.schema import account_identifier, archive_skip, document
from rheo_finance.tier import Tier

P = Tier.PUBLIC
ARCHIVE_KEY: Final = "finance.paths.archive"
# derived_id needs a business date and a document has none of its own at registration;
# the registry rows of stage 2 use the same fixed date.
REGISTRY_DATE: Final = dt.date(2000, 1, 1)
# What a stored path becomes once the adopted-path check refuses it (an identifier
# registered after the file was): no parser opens it, and the document takes its real
# path back when the walk next finds its bytes at an adoptable one.
WITHHELD_PATH: Final = "(withheld)"

SkipReason = Literal[
    "identifier_in_path", "unmapped_directory", "not_a_regular_file", "not_downloaded",
    "unreadable",
]  # fmt: skip
DocumentStatus = Literal[
    "filed", "parsed", "rejected", "unparsed", "needs_manual_entry", "superseded"
]
DOCUMENT_STATUSES: Final[tuple[DocumentStatus, ...]] = (
    "filed", "parsed", "rejected", "unparsed", "needs_manual_entry", "superseded",
)  # fmt: skip

_ICLOUD_PLACEHOLDER = re.compile(r"\..+\.icloud")


class DocumentRefused(OperationError):
    """The walk cannot run. Messages name the setting or the manual file's name and
    what to fix, never a path or a cell's value."""


def document_id(sha256: str) -> uuid.UUID:
    return derived_id(REGISTRY_DATE, ("document", sha256))


def _identifiers_file_refused(line_no: int, what: str) -> DocumentRefused:
    """The refusal for a malformed identifiers file: its name, the line and what is
    wrong there, and the remedy. Never a cell's value or the file's path."""
    return DocumentRefused(
        f"{IDENTIFIERS_FILE} line {line_no}: {what}; nothing was filed, fix the file"
        " first"
    )


def file_identifiers(data_dir: Path) -> set[str]:
    """The identifier text of every row of ``<data>/manual/account_identifiers.csv``
    whose kind is not ``internal``, read only.

    The rows ``load_csv`` validates, and the identifier cell of every other row too:
    a row the registry rejects (a bad date, an unknown account, too few cells) still
    names a real identifier, and a path carrying it must not be stored. A missing
    file or a header-only file gives none.

    Fails closed. The file is refused with ``DocumentRefused`` when its first line
    is not exactly the expected header, when ``load_csv`` reports a file-level error
    or a row that is not valid CSV, or when any cell holds a line break: each of
    those can lose an identifier row (a headerless file's first row would pass as
    the header; an unclosed quote swallows every later row), and a lost identifier
    lets a path carrying it be stored. The owner fixes the file; nothing is filed
    until then.
    """
    path = data_dir / MANUAL_DIR_NAME / IDENTIFIERS_FILE
    columns = MANUAL_FILES[IDENTIFIERS_FILE]
    loaded = load_csv(path, AccountIdentifierRow)
    for error in sorted(loaded.errors):
        if error.line_no == 1 or error.reason == INVALID_CSV:
            raise _identifiers_file_refused(error.line_no, error.reason)
    found = {row.identifier for row in loaded.rows if row.kind != "internal"}
    try:
        text = path.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        return found
    # Exactly the header ``finance init-data`` writes: ``load_csv`` also accepts one
    # with padded cells, but a file that is not what the tooling wrote is not trusted.
    first_line = text.splitlines()[:1]
    if first_line != [",".join(columns)]:
        raise _identifiers_file_refused(1, f"the header is not: {','.join(columns)}")

    at = columns.index("identifier")
    kind_at = columns.index("kind")

    def cell(cells: list[str], index: int) -> str:
        return cells[index].strip() if index < len(cells) else ""

    reader = csv.reader(io.StringIO(text, newline=""), strict=True)
    next(reader)  # the header, checked above
    while True:
        start = reader.line_num + 1
        try:
            cells = next(reader, None)
        except csv.Error:
            raise _identifiers_file_refused(start, INVALID_CSV) from None
        if cells is None:
            break
        if any("\n" in each or "\r" in each for each in cells):
            raise _identifiers_file_refused(start, "a cell holds a line break")
        identifier = cell(cells, at)
        if identifier and cell(cells, kind_at) != "internal":
            found.add(identifier)
    return found


def registered_identifiers(uow: UnitOfWork, data_dir: Path) -> tuple[str, ...]:
    """The identifiers the adopted-path check refuses: those in the manual
    identifiers file (``file_identifiers``) and those in ``account_identifier``,
    without the built-in account's ``internal`` one."""
    stored = set(
        uow.scalars(
            select(account_identifier.c.identifier).where(
                account_identifier.c.kind != "internal"
            )
        )
    )
    return tuple(sorted(stored | file_identifiers(data_dir)))


# --- the walk -------------------------------------------------------------------------


@dataclass(frozen=True)
class Found:
    """One adoptable file: where it sits and what the map says about it."""

    relative: str
    placement: Placement
    sha256: str


@dataclass
class Walk:
    found: list[Found] = field(default_factory=list)
    skipped: Counter[tuple[str, SkipReason]] = field(default_factory=Counter)


def _entries(root: Path, on_error: Callable[[OSError], None]) -> Iterator[Path]:
    """Every non-hidden entry under ``root`` but the directories walked into, in sorted
    order. A symlinked directory is yielded, not followed, so the walk counts it. A
    directory that cannot be listed goes to ``on_error``."""
    for current, dirs, names in os.walk(root, onerror=on_error, followlinks=False):
        base = Path(current)
        shown = sorted(name for name in dirs if not name.startswith("."))
        dirs[:] = [name for name in shown if not (base / name).is_symlink()]
        linked = [name for name in shown if (base / name).is_symlink()]
        for name in sorted([*names, *linked]):
            if not name.startswith(".") or _ICLOUD_PLACEHOLDER.fullmatch(name):
                yield base / name


def _sha256(path: Path) -> str:
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def walk(root: Path, check: PathCheck) -> Walk:
    """Read the archive under ``root``; write nothing."""
    result = Walk()

    def unlisted(error: OSError) -> None:
        # Counted by mapped directory only; the error and its file name go no further.
        where = Path(str(error.filename)) if error.filename else root
        try:
            relative = PurePosixPath(where.relative_to(root).as_posix())
        except ValueError:
            relative = PurePosixPath(".")
        result.skipped[skip_directory(relative / "_"), "unreadable"] += 1

    for path in _entries(root, unlisted):
        relative = PurePosixPath(path.relative_to(root).as_posix())
        directory = skip_directory(relative)
        # The identifier check comes first: whatever else is wrong with a path that
        # carries one, it is counted as that and its path goes no further.
        if not check.allows(str(relative)):
            result.skipped[directory, "identifier_in_path"] += 1
            continue
        if path.is_symlink() or not path.is_file():
            # A link could point outside the archive, and a pipe or socket would
            # block the read.
            result.skipped[directory, "not_a_regular_file"] += 1
            continue
        if _ICLOUD_PLACEHOLDER.fullmatch(relative.name):
            result.skipped[directory, "not_downloaded"] += 1
            continue
        placement = place(relative)
        if placement is None:
            result.skipped[directory, "unmapped_directory"] += 1
            continue
        try:
            sha = _sha256(path)
        except OSError:
            # Vanished or unreadable since it was listed: counted, never named.
            result.skipped[directory, "unreadable"] += 1
            continue
        result.found.append(Found(str(relative), placement, sha))
    return result


# --- the registry ---------------------------------------------------------------------


@dataclass(frozen=True)
class Filed:
    registered: int
    known: int
    moved: int
    duplicates: int
    withheld: int
    skipped: tuple[tuple[str, SkipReason, int], ...]


def _new_row(found: Found, now: dt.datetime) -> dict[str, object]:
    kind = found.placement.kind
    status: DocumentStatus = "needs_manual_entry" if kind == "image" else "filed"
    return {
        "id": document_id(found.sha256),
        "path": found.relative,
        "sha256": found.sha256,
        "kind": kind,
        "issuer": found.placement.issuer,
        "format": found.placement.format,
        "status": status,
        "extraction": "transcription" if kind == "drop_record" else "none",
        "imported_at": now,
    }


def file_archive(
    uow: UnitOfWork, root: Path, data_dir: Path, now: dt.datetime
) -> Filed:
    """Walk the archive at ``root``, register new bytes, follow moved ones, withhold
    stored paths the check now refuses and rewrite ``archive_skip``."""
    check = PathCheck(registered_identifiers(uow, data_dir))
    walked = walk(root, check)
    paths_by_sha: dict[str, list[Found]] = {}
    for found in walked.found:
        paths_by_sha.setdefault(found.sha256, []).append(found)
    stored = {
        row.sha256: row.path
        for row in uow.execute(
            select(document.c.sha256, document.c.path).where(
                document.c.sha256.in_(list(paths_by_sha))
            )
        )
    }

    new_rows: list[dict[str, object]] = []
    known = moved = 0
    for sha, places in paths_by_sha.items():
        first = places[0]
        if sha not in stored:
            new_rows.append(_new_row(first, now))
        elif stored[sha] in {each.relative for each in places}:
            known += 1
        else:
            uow.execute(
                update(document)
                .where(document.c.sha256 == sha)
                .values(path=first.relative, issuer=first.placement.issuer)
            )
            moved += 1
    if new_rows:
        uow.execute(insert(document), new_rows)
    withheld = _withhold_refused(uow, check)

    uow.execute(delete(archive_skip))
    skipped = tuple(
        (directory, reason, count)
        for (directory, reason), count in sorted(walked.skipped.items())
    )
    if skipped:
        uow.execute(
            insert(archive_skip),
            [
                {"directory": directory, "reason": reason, "count": count}
                for directory, reason, count in skipped
            ],
        )
    return Filed(
        registered=len(new_rows),
        known=known,
        moved=moved,
        duplicates=len(walked.found) - len(paths_by_sha),
        withheld=withheld,
        skipped=skipped,
    )


def _withhold_refused(uow: UnitOfWork, check: PathCheck) -> int:
    """Withhold every stored path that ``check`` refuses now.

    A path stored before an identifier in it was registered must not stay stored. The
    row keeps its bytes' hash and its status; its path becomes ``WITHHELD_PATH`` and
    its issuer the mapped top-level directory (a nested issuer is part of the path, so
    it is refused with it). The file itself, still at that path, is
    counted in ``archive_skip`` by the same walk. Runs after the moves, so bytes found
    at an adoptable path are followed there instead.
    """
    refused = [
        (row.id, row.issuer)
        for row in uow.execute(
            select(document.c.id, document.c.path, document.c.issuer).where(
                document.c.path != WITHHELD_PATH
            )
        )
        if not check.allows(row.path)
    ]
    for document_row_id, issuer in refused:
        uow.execute(
            update(document)
            .where(document.c.id == document_row_id)
            .values(path=WITHHELD_PATH, issuer=issuer.split("/")[0])
        )
    return len(refused)


# --- finance.document.file ------------------------------------------------------------


class FileIn(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class SkipCount(BaseModel):
    model_config = ConfigDict(frozen=True)

    directory: Annotated[str, Tiered(P)]
    reason: Annotated[SkipReason, Tiered(P)]
    count: Annotated[int, Tiered(P)]


class DownloadsOut(BaseModel):
    """What the Downloads filer did (``documents.filer``), as counts only.
    ``scanned`` is false when ``finance.paths.downloads`` does not exist."""

    model_config = ConfigDict(frozen=True)

    scanned: Annotated[bool, Tiered(P)]
    filed: Annotated[int, Tiered(P)]
    known: Annotated[int, Tiered(P)]
    # The name the date gives was refused by the adopted-path check; not filed.
    refused: Annotated[int, Tiered(P)]
    # Of a format a filing rule reads, but no rule could date it; not filed.
    unrecognised: Annotated[int, Tiered(P)]
    unreadable: Annotated[int, Tiered(P)]
    # VCTY_*.pdf and VirginPDF*.pdf, left to the monitors; waiting: a day or more old.
    monitor_skipped: Annotated[int, Tiered(P)]
    monitor_waiting: Annotated[int, Tiered(P)]


class FileOut(BaseModel):
    """Counts only: no path appears in the output."""

    model_config = ConfigDict(frozen=True)

    registered: Annotated[int, Tiered(P)]
    known: Annotated[int, Tiered(P)]
    moved: Annotated[int, Tiered(P)]
    # Second and later paths holding bytes already found in this walk.
    duplicates: Annotated[int, Tiered(P)]
    # Stored paths replaced by WITHHELD_PATH in this walk.
    withheld: Annotated[int, Tiered(P)]
    skipped: Annotated[list[SkipCount], Tiered(P)]
    downloads: Annotated[DownloadsOut, Tiered(P)]


def file_documents(ctx: FinanceContext, uow: UnitOfWork, _data: FileIn) -> FileOut:
    """File new Downloads into the archive, then walk the archive, so a file filed
    now is registered by the same run."""
    # Imported here: the filer reads the parser registry, which imports this module.
    from rheo_finance.documents import filer

    root = ctx.settings.get_path(ARCHIVE_KEY)
    if root is None or not root.is_dir():
        raise DocumentRefused(f"the {ARCHIVE_KEY} directory does not exist")
    now = ctx.clock.now_utc()
    check = PathCheck(registered_identifiers(uow, ctx.paths.data))
    downloads = filer.file_downloads(
        uow, ctx.settings.get_path(filer.DOWNLOADS_KEY), root, check, now
    )
    filed = file_archive(uow, root, ctx.paths.data, now)
    return FileOut(
        registered=filed.registered,
        known=filed.known,
        moved=filed.moved,
        duplicates=filed.duplicates,
        withheld=filed.withheld,
        skipped=[
            SkipCount(directory=directory, reason=reason, count=count)
            for directory, reason, count in filed.skipped
        ],
        downloads=DownloadsOut(
            scanned=downloads.scanned,
            filed=downloads.filed,
            known=downloads.known,
            refused=len(downloads.refused),
            unrecognised=len(downloads.unrecognised),
            unreadable=downloads.unreadable,
            monitor_skipped=downloads.monitor_skipped,
            monitor_waiting=downloads.monitor_waiting,
        ),
    )


register(
    OperationSpec(
        name="finance.document.file",
        input_model=FileIn,
        output_model=FileOut,
        handler=file_documents,
        safety_class=SafetyClass.MUTATE,
        roles=frozenset({Role.OWNER, Role.SERVICE}),
        connection="owner",
        description=(
            "Copy dateable new files from Downloads into the archive (never the"
            " monitors' files), then adopt the archive in place: register each new"
            " file by sha256 and count the files whose path carries an identifier,"
            " by directory only."
        ),
    )
)
