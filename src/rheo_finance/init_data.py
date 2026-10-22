"""``finance init-data``: lay out ``$FINANCE_DATA_DIR`` (default ``~/Finances``).

Creates the data directory and its subdirectories (mode 0700), a fully commented
``settings.toml`` and the header-only manual-entry CSVs under ``manual/``. Idempotent:
a file that exists is never rewritten, so a second run leaves the owner's edits alone. A
directory it manages that is readable by group or others is tightened to 0700.
"""

from __future__ import annotations

import argparse
import csv
import io
import os
import stat
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

from rheo_finance import db
from rheo_finance.manual.headers import MANUAL_DIR_NAME, MANUAL_FILES
from rheo_finance.render import Report, emit
from rheo_finance.settings import SETTINGS_FILE_NAME, settings_template

DIRECTORIES: Final[tuple[str, ...]] = (
    "mail-inbox",
    "mail-inbox/eml",
    "mail-inbox/drops",
    "dumps",
    "staging",
    "packs",
    "logs",
    MANUAL_DIR_NAME,
)
PRIVATE_DIR_MODE: Final = 0o700
PRIVATE_FILE_MODE: Final = 0o600


@dataclass
class InitDataResult:
    data_dir: Path
    created: list[Path] = field(default_factory=list)
    kept: list[Path] = field(default_factory=list)
    tightened: list[Path] = field(default_factory=list)


def header_line(columns: tuple[str, ...]) -> str:
    buffer = io.StringIO()
    csv.writer(buffer, lineterminator="\n").writerow(columns)
    return buffer.getvalue()


def _ensure_dir(path: Path, result: InitDataResult) -> None:
    if path.is_dir():
        result.kept.append(path)
        if stat.S_IMODE(path.stat().st_mode) & 0o077:
            path.chmod(PRIVATE_DIR_MODE)
            result.tightened.append(path)
        return
    path.mkdir(mode=PRIVATE_DIR_MODE, parents=True)
    path.chmod(PRIVATE_DIR_MODE)  # mkdir's mode is filtered by the umask
    result.created.append(path)


def _ensure_file(path: Path, body: str, result: InitDataResult) -> None:
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, PRIVATE_FILE_MODE)
    except FileExistsError:
        result.kept.append(path)
        return
    with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
        handle.write(body)
    result.created.append(path)


def init_data(data: Path | None = None) -> InitDataResult:
    directory = data or db.data_dir()
    result = InitDataResult(data_dir=directory)
    _ensure_dir(directory, result)
    for name in DIRECTORIES:
        _ensure_dir(directory / name, result)
    _ensure_file(directory / SETTINGS_FILE_NAME, settings_template(), result)
    for name, columns in MANUAL_FILES.items():
        _ensure_file(directory / MANUAL_DIR_NAME / name, header_line(columns), result)
    return result


def _cmd_init_data(_args: argparse.Namespace) -> int:
    try:
        result = init_data()
    except OSError as exc:
        emit(f"finance init-data: failed. {exc}", sys.stderr)
        return 1
    if result.created:
        headline = (
            f"finance init-data: created {len(result.created)} item(s) in"
            f" {result.data_dir}; left {len(result.kept)} existing item(s)"
            " as they were."
        )
    else:
        headline = (
            f"finance init-data: {result.data_dir} is already set up;"
            f" left {len(result.kept)} existing item(s) as they were."
        )
    tightened = [f"Tightened {path} to mode 0700." for path in result.tightened]
    emit(Report([headline], tightened))
    return 0


def add_parser(commands: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    commands.add_parser(
        "init-data",
        help="create the data directory, settings.toml and the manual CSVs",
        description=(
            "Create $FINANCE_DATA_DIR (default ~/Finances) with a commented"
            " settings.toml, header-only manual-entry CSVs and its subdirectories."
            " Existing files are never overwritten."
        ),
    ).set_defaults(handler=_cmd_init_data)
