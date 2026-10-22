"""The archive walk and the document registry, and ``finance.document.file`` (FR-2).

The walk adopts the files under ``finance.paths.archive`` where they sit: it opens
each one for reading only, to hash it, and never moves, renames or writes one. Every
file gets one of three outcomes.

- **Skipped and counted.** The adopted-path check (``PathCheck``) runs on the
  archive-relative path before anything else: a path carrying a registered account
  identifier, or a run of 8 or more digits that is not a ``YYYYMMDD`` date, is not
  registered. Neither is a symlink or anything else that is not a regular file, a file
  outside every mapped directory (``issuers.ISSUER_DIRS``), or an iCloud placeholder
  whose bytes are not on disk. Each lands in ``archive_skip`` as a count by issuer
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

import datetime as dt
import hashlib
import os
import re
import unicodedata
import uuid
from collections import Counter
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Annotated, Final, Literal

from pydantic import BaseModel, ConfigDict
from sqlalchemy import delete, insert, select, update

from rheo_finance.context import FinanceContext, UnitOfWork
from rheo_finance.contracts import Role, SafetyClass, Tiered
from rheo_finance.documents.issuers import Placement, place, skip_directory
from rheo_finance.operations import OperationError, OperationSpec, register
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
    "identifier_in_path", "unmapped_directory", "not_a_regular_file", "not_downloaded"
]
DocumentStatus = Literal[
    "filed", "parsed", "rejected", "unparsed", "needs_manual_entry", "superseded"
]
DOCUMENT_STATUSES: Final[tuple[DocumentStatus, ...]] = (
    "filed", "parsed", "rejected", "unparsed", "needs_manual_entry", "superseded",
)  # fmt: skip

_DIGIT_RUN = re.compile(r"\d+")
# Digit runs joined by single separators, such as ``999-1234`` or ``999 1234``.
_DIGIT_GROUP = re.compile(r"\d+(?:[\s._-]\d+)+")
# An identifier that is a bare number: digits with optional spaces, dots or hyphens.
_NUMBER = re.compile(r"[\d\s.-]*\d[\d\s.-]*")
_SEPARATORS = re.compile(r"[\s._-]+")
_MASKS = re.compile(r"[*x#\u2022]")
_ICLOUD_PLACEHOLDER = re.compile(r"\..+\.icloud")
# The years a YYYYMMDD run may name and still pass as a date. Narrower than every
# calendar date on purpose: a number that only parses as a far-off date is skipped.
_DATE_YEARS: Final = range(1900, 2101)


class DocumentRefused(OperationError):
    """The walk cannot run. Messages name the setting, never a path."""


def document_id(sha256: str) -> uuid.UUID:
    return derived_id(REGISTRY_DATE, ("document", sha256))


# --- the adopted-path check -----------------------------------------------------------


def _comparable(value: str) -> str:
    """``value`` NFKC-normalized (macOS file names arrive decomposed), casefolded,
    without spaces, dots, underscores or hyphens, and with every mask character
    (``*``, ``x``, ``#``, a bullet) made ``*``: so ``My_Wallet`` matches
    ``my wallet``, and ``9999-99xx-xxxx-4321`` matches ``9999 99** **** 4321``."""
    folded = unicodedata.normalize("NFKC", value).casefold()
    return _MASKS.sub("*", _SEPARATORS.sub("", folded))


def _is_date(run: str) -> bool:
    if len(run) != 8:
        return False
    try:
        day = dt.date(int(run[:4]), int(run[4:6]), int(run[6:]))
    except ValueError:
        return False
    return day.year in _DATE_YEARS


class PathCheck:
    """Whether an archive-relative path may be stored (Privacy and safety mechanics,
    "Adopted paths carry no identifier").

    A path is refused when it holds a run of 8 or more digits that is not a
    ``YYYYMMDD`` date, or contains a registered identifier. An identifier that is a bare
    number (digits with spaces, dots or hyphens) matches when its digits appear inside
    one digit run of the path, or when it equals two or more consecutive whole runs of
    one separated group (``999-1234`` for ``9991234``; a run is never split, so a
    date such as ``2024-01-31`` does not hide a number from its middle). Any other
    identifier (a masked card, a file token, a
    portfolio name) matches when it appears in the path, both compared without case,
    separators or the difference between mask characters. Both are substring
    matches: they err toward skipping, and a skipped file is adopted once renamed.
    """

    def __init__(self, identifiers: Iterable[str]) -> None:
        numbers: set[str] = set()
        texts: set[str] = set()
        for identifier in identifiers:
            if _NUMBER.fullmatch(identifier):
                numbers.add("".join(ch for ch in identifier if ch.isdigit()))
            elif comparable := _comparable(identifier):
                texts.add(comparable)
        self._numbers = frozenset(numbers)
        self._texts = frozenset(texts)

    def allows(self, relative: str) -> bool:
        runs = _DIGIT_RUN.findall(relative)
        if any(len(run) >= 8 and not _is_date(run) for run in runs):
            return False
        if any(number in run for run in runs for number in self._numbers):
            return False
        if self._numbers and any(
            joined in self._numbers
            for group in _DIGIT_GROUP.findall(relative)
            for joined in _joined_runs(_DIGIT_RUN.findall(group))
        ):
            return False
        comparable = _comparable(relative)
        return not any(text in comparable for text in self._texts)


def _joined_runs(runs: list[str]) -> Iterator[str]:
    """Every concatenation of two or more consecutive runs."""
    for start in range(len(runs)):
        for end in range(start + 2, len(runs) + 1):
            yield "".join(runs[start:end])


def registered_identifiers(uow: UnitOfWork) -> tuple[str, ...]:
    """Every identifier in ``account_identifier`` (loaded by
    ``manual.stage2.load_accounts``) but the built-in account's ``internal`` one."""
    return tuple(
        uow.scalars(
            select(account_identifier.c.identifier).where(
                account_identifier.c.kind != "internal"
            )
        )
    )


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


def _entries(root: Path) -> Iterator[Path]:
    """Every non-hidden entry under ``root`` but the directories walked into, in sorted
    order. A symlinked directory is yielded, not followed, so the walk counts it."""
    for current, dirs, names in os.walk(root, followlinks=False):
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
    for path in _entries(root):
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
        result.found.append(Found(str(relative), placement, _sha256(path)))
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


def file_archive(uow: UnitOfWork, root: Path, now: dt.datetime) -> Filed:
    """Walk the archive at ``root``, register new bytes, follow moved ones, withhold
    stored paths the check now refuses and rewrite ``archive_skip``."""
    check = PathCheck(registered_identifiers(uow))
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


def file_documents(ctx: FinanceContext, uow: UnitOfWork, _data: FileIn) -> FileOut:
    root = ctx.settings.get_path(ARCHIVE_KEY)
    if root is None or not root.is_dir():
        raise DocumentRefused(f"the {ARCHIVE_KEY} directory does not exist")
    filed = file_archive(uow, root, ctx.clock.now_utc())
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
            "Adopt the archive in place: register each new file by sha256 and count"
            " the files whose path carries an identifier, by directory only."
        ),
    )
)
