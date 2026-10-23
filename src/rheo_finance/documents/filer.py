"""The Downloads filer, the first step of ``finance.document.file`` (FR-2, Key
decision 2).

It looks at the regular, non-hidden files directly under ``finance.paths.downloads``
and writes nothing there: it opens a file there for reading only, and never deletes,
moves or renames one. Each file it looks at ends one of these ways:

- **Monitor file.** ``VCTY_*.pdf`` and ``VirginPDF*.pdf`` belong to the existing
  monitors, which file them so their tickets still fire; the ledger adopts them from
  the archive. The filer never opens one. One whose mtime is at least
  ``MONITOR_WAIT`` old is counted as waiting (status line 10).
- **Filed.** The file goes to every registered parser that reads its format and has a
  ``filing_date``, in parser id order, and the first that reads a date names the copy
  ``<issuer-dir>/<YYYY-MM-DD>.<ext>`` (the statement's period end or the bill's date),
  or ``<YYYY-MM-DD>-2.<ext>``, ``-3`` and so on when that name holds other bytes
  (SPEC "Files, paths and settings" > Archive). A filed name is built from the date
  alone: nothing of the downloaded file's own name, which is free text, reaches it.
  The copy holds exactly the bytes that were hashed.
- **Known.** Bytes already registered in ``document``, already at the name the date
  gives, or met earlier in the same scan are not copied again, so filing the same
  bytes twice adds nothing.
- **Refused.** The adopted-path check (``path_check.PathCheck``) runs on the
  archive-relative name before the copy. A name it refuses (the date's digits make a
  registered identifier) is not written, and the file stays where it is.
- **Unrecognised.** A file of a format some filing rule reads (pdf, csv) that no rule
  dates, including a PDF with no text layer, stays where it is. A file of any other
  format is not a candidate and is passed over.
- **Unreadable.** A file that vanished or cannot be read mid-scan is counted.

Refused and unrecognised files are listed by their name in Downloads in the returned
``DownloadsFiled``, for an in-process caller only: the operation's output carries
counts, and no Downloads name goes into a table, an event or an error message.
"""

from __future__ import annotations

import csv
import datetime as dt
import hashlib
import itertools
import os
import stat
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Final

from sqlalchemy import select

from rheo_finance.context import UnitOfWork
from rheo_finance.documents.issuers import file_format
from rheo_finance.parsers.base import NoTextLayer, NotRecognised
from rheo_finance.parsers.docextract_adapter import pdf_text
from rheo_finance.parsers.registry import Parser, load_parsers
from rheo_finance.path_check import PathCheck
from rheo_finance.storage.schema import document

DOWNLOADS_KEY: Final = "finance.paths.downloads"
# The monitors' own patterns (Key decision 2), matched case-sensitively as they are.
MONITOR_PATTERNS: Final = ("VCTY_*.pdf", "VirginPDF*.pdf")
# How long a monitor file sits in Downloads before status reports it.
MONITOR_WAIT: Final = dt.timedelta(hours=24)


@dataclass(frozen=True)
class DownloadsFiled:
    """What one scan did. ``scanned`` is false when the Downloads directory does not
    exist or cannot be listed, and then every count is zero."""

    scanned: bool = False
    filed: int = 0
    known: int = 0
    unreadable: int = 0
    monitor_skipped: int = 0
    monitor_waiting: int = 0
    # Names in Downloads, in sorted order. Never output, stored or logged.
    refused: tuple[str, ...] = ()
    unrecognised: tuple[str, ...] = ()


def is_monitor_file(name: str) -> bool:
    return any(fnmatchcase(name, pattern) for pattern in MONITOR_PATTERNS)


def _files(downloads: Path) -> Iterator[tuple[Path, os.stat_result]]:
    """Every regular, non-hidden file directly under ``downloads``, sorted by name,
    with its lstat. A symlink is not followed: it could point anywhere."""
    for entry in sorted(downloads.iterdir()):
        if entry.name.startswith("."):
            continue
        try:
            info = entry.lstat()
        except OSError:
            continue
        if stat.S_ISREG(info.st_mode):
            yield entry, info


def _waiting(info: os.stat_result, now: dt.datetime) -> bool:
    modified = dt.datetime.fromtimestamp(info.st_mtime, dt.UTC)
    return now - modified >= MONITOR_WAIT


def monitor_files_waiting(downloads: Path | None, now: dt.datetime) -> int:
    """How many monitor-pattern files have sat in ``downloads`` for ``MONITOR_WAIT``
    or longer, by mtime; zero when the directory does not exist. Reads names and
    mtimes only, so status can call it without the filer."""
    if downloads is None or not downloads.is_dir():
        return 0
    try:
        return sum(
            1
            for path, info in _files(downloads)
            if is_monitor_file(path.name) and _waiting(info, now)
        )
    except OSError:
        return 0  # a directory that cannot be listed has no waiting files to report


def filing_rules() -> tuple[Parser, ...]:
    """The registered parsers that can date a file, in parser id order."""
    return tuple(
        sorted(
            (each for each in load_parsers().values() if each.filing_date is not None),
            key=lambda each: each.parser_id,
        )
    )


def _text(path: Path, data: bytes, fmt: str, extract_pdf: Callable[[Path], str]) -> str:
    """The text the parsers read; ``NotRecognised`` when there is none."""
    if fmt == "pdf":
        try:
            return extract_pdf(path)
        except NoTextLayer:
            raise NotRecognised("no text layer") from None
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise NotRecognised("not text") from None


def _dated(
    path: Path,
    data: bytes,
    fmt: str,
    rules: tuple[Parser, ...],
    extract_pdf: Callable[[Path], str],
) -> tuple[Parser, dt.date] | None:
    """The first rule that dates the file, and the date; None when none does.

    A rule that matches the layout but finds a field it cannot read (a date that is not
    a date, an amount that is not a number, a CSV cell over the reader's size limit)
    raises ``ValueError``, ``ArithmeticError`` or ``csv.Error`` out of its parse: that
    means this rule cannot date this file, so the next rule tries, and the file is
    unrecognised when none does. One bad file must not stop the scan.
    """
    text: str | None = None
    for rule in rules:
        if rule.format != fmt or rule.filing_date is None:
            continue
        if text is None:
            try:
                text = _text(path, data, fmt, extract_pdf)
            except NotRecognised:
                return None
        try:
            return rule, rule.filing_date(text)
        except (NotRecognised, ValueError, ArithmeticError, csv.Error):
            continue
    return None


def filed_names(day: dt.date, fmt: str) -> Iterator[str]:
    """``<YYYY-MM-DD>.<ext>``, then the ``-2``, ``-3``, ... names for a clash."""
    yield f"{day.isoformat()}.{fmt}"
    for number in itertools.count(2):
        yield f"{day.isoformat()}-{number}.{fmt}"


def _sha256_at(path: Path) -> str | None:
    """The sha256 of the regular file at ``path``; None for anything else."""
    try:
        if not stat.S_ISREG(path.lstat().st_mode):
            return None
        with path.open("rb") as handle:
            return hashlib.file_digest(handle, "sha256").hexdigest()
    except OSError:
        return None


def _write_new(path: Path, data: bytes) -> bool:
    """Create ``path`` holding ``data``; False when something already sits there.
    A copy that fails part way is removed, so no half-written file is adopted."""
    try:
        handle = path.open("xb")
    except FileExistsError:
        return False
    try:
        with handle:
            handle.write(data)
    except BaseException:
        # Only ever the file this call just created; never an original.
        path.unlink(missing_ok=True)
        raise
    return True


def _place(
    archive: Path, issuer: str, day: dt.date, fmt: str, data: bytes, sha: str,
    check: PathCheck,
) -> str:  # fmt: skip
    """Copy ``data`` under the first free name the date gives: ``filed``,
    ``known`` when a name already holds these bytes, or ``refused``."""
    directory = archive / issuer
    for name in filed_names(day, fmt):
        target = directory / name
        if os.path.lexists(target):
            if _sha256_at(target) == sha:
                return "known"
            continue
        if not check.allows(f"{issuer}/{name}"):
            return "refused"
        directory.mkdir(exist_ok=True)
        if _write_new(target, data):
            return "filed"
    raise AssertionError("unreachable: filed_names never ends")


def file_downloads(
    uow: UnitOfWork,
    downloads: Path | None,
    archive: Path,
    check: PathCheck,
    now: dt.datetime,
    *,
    extract_pdf: Callable[[Path], str] | None = None,
) -> DownloadsFiled:
    """Copy the dateable files in ``downloads`` into ``archive``.

    ``extract_pdf`` turns a PDF into its layout text; by default ``pdf_text``,
    looked up when the scan runs.
    """
    if downloads is None or not downloads.is_dir():
        return DownloadsFiled()
    extract = extract_pdf or pdf_text
    try:
        # Listed before anything is filed: a directory that cannot be listed (no
        # permission) is not scanned, and the archive walk still runs.
        entries = list(_files(downloads))
    except OSError:
        return DownloadsFiled()
    rules = filing_rules()
    formats = {rule.format for rule in rules}
    seen: set[str] = set()
    counts = {"filed": 0, "known": 0, "unreadable": 0, "skipped": 0, "waiting": 0}
    refused: list[str] = []
    unrecognised: list[str] = []
    for path, info in entries:
        if is_monitor_file(path.name):
            counts["skipped"] += 1
            counts["waiting"] += _waiting(info, now)
            continue
        fmt = file_format(path.name)
        if fmt not in formats:
            continue
        try:
            data = path.read_bytes()
        except OSError:
            counts["unreadable"] += 1
            continue
        sha = hashlib.sha256(data).hexdigest()
        if sha in seen or uow.scalar(
            select(document.c.id).where(document.c.sha256 == sha)
        ):
            counts["known"] += 1
            continue
        try:
            dated = _dated(path, data, fmt, rules, extract)
        except OSError:
            counts["unreadable"] += 1
            continue
        if dated is None:
            unrecognised.append(path.name)
            continue
        rule, day = dated
        outcome = _place(archive, rule.issuer, day, fmt, data, sha, check)
        if outcome == "refused":
            refused.append(path.name)
            continue
        seen.add(sha)
        counts[outcome] += 1
    return DownloadsFiled(
        scanned=True,
        filed=counts["filed"],
        known=counts["known"],
        unreadable=counts["unreadable"],
        monitor_skipped=counts["skipped"],
        monitor_waiting=counts["waiting"],
        refused=tuple(refused),
        unrecognised=tuple(unrecognised),
    )
