"""Repo check (FR-45): fail when a tracked or staged file looks like private data.

Usage: ``python scripts/repo_check.py --staged`` (the pre-commit hook) or ``--tracked``.
Exits 0 when clean and 1 on any failure. Standard library only, Python 3.11 or newer.

Rule 1, over every file checked:
  * a statement-type extension outside ``tests/fixtures/synthetic/`` fails (the
    spec's list plus jpeg, png, xlsx, xls and zip, a deliberate tightening);
  * a file under ``tests/fixtures/synthetic/`` not listed in its ``MANIFEST.toml``
    fails.
Rule 2, over data-shaped files only (everything under ``tests/fixtures/`` and
``src/rheo_finance/reference/``, plus csv, tsv, json, txt and eml files anywhere):
  * a run of 8 or more digits bounded by non-alphanumerics that does not start with the
    fake prefix ``999`` fails, unless it is a ``YYYYMMDD`` calendar date or a 13-digit
    millisecond epoch (``1`` then 12 digits);
  * a Luhn-valid 13 to 19 digit number fails, written contiguously or split into
    card groups (4-4-4-4, 4-4-4-4-1 to 3, 4-6-5, 4-6-4) with spaces or hyphens
    between them. Every such window is checked wherever it sits among other groups,
    so a neighbouring number cannot hide a card. The contiguous 13-digit epoch shape
    is never Luhn-checked.
  * a data-shaped file that holds NUL bytes and does not decode as UTF-16 or UTF-32
    fails as unscannable; UTF-16 and UTF-32 files are decoded and scanned.
The epoch allow-pattern is exactly ``1[0-9]{12}`` (2001 to 2033); do not widen it.
Fractional digit runs (an 8-decimal quantity) are digit runs too and need the 999
prefix in fixtures. Claude Code's settings files (``.claude/*.json``) are not checked.

Content is read from the git index, so ``--staged`` checks exactly what the commit
will hold. Failure output names the path, the line number and the rule. It never
prints the matched digits.
"""

from __future__ import annotations

import argparse
import datetime
import os
import re
import subprocess
import sys
import tomllib
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import PurePosixPath

SYNTHETIC_DIR = "tests/fixtures/synthetic/"
MANIFEST_PATH = SYNTHETIC_DIR + "MANIFEST.toml"
REFERENCE_DIR = "src/rheo_finance/reference/"
DATA_DIRS = ("tests/fixtures/", REFERENCE_DIR)

STATEMENT_EXTENSIONS = frozenset(
    {".pdf", ".csv", ".eml", ".dump", ".sql", ".qfx", ".ofx", ".jsonl", ".heic", ".jpg"}
    # Beyond the spec's list on purpose: other image, spreadsheet and archive exports.
    | {".jpeg", ".png", ".xlsx", ".xls", ".zip"}
)
DATA_EXTENSIONS = frozenset({".csv", ".tsv", ".json", ".txt", ".eml"})

FAKE_PREFIX = b"999"
# A run of digits with no letter or digit on either side, so a run inside a hex hash
# or an identifier does not match.
DIGIT_RUN = re.compile(rb"(?<![A-Za-z0-9])[0-9]{8,}(?![A-Za-z0-9])")
# Grouped cards: bounded digit runs with only spaces or hyphens between them, checked
# only in the printed card layouts below. A group longer than 6 digits ends the chain,
# and no layout matches ISO dates (4-2-2) or rows of plain 3-digit numbers.
BOUNDED_RUN = re.compile(rb"(?<![A-Za-z0-9])[0-9]+(?![A-Za-z0-9])")
GROUP_GAP = re.compile(rb"[ -]+")
CARD_LAYOUTS = (
    (4, 4, 4, 4),
    (4, 4, 4, 4, 1),
    (4, 4, 4, 4, 2),
    (4, 4, 4, 4, 3),
    (4, 6, 5),
    (4, 6, 4),
)
EPOCH_MS = re.compile(rb"1[0-9]{12}")

RULE_STATEMENT = "statement-type file outside tests/fixtures/synthetic/"
RULE_UNLISTED = "fixture not listed in MANIFEST.toml"
RULE_MANIFEST = "MANIFEST.toml does not parse"
RULE_DIGITS = "run of 8 or more digits that is not 999-prefixed, a date or an epoch"
RULE_CARD = "Luhn-valid card-shaped number"
RULE_UNSCANNABLE = "data file holds NUL bytes and does not decode as UTF-16 or UTF-32"

UTF32_BOMS = (b"\xff\xfe\x00\x00", b"\x00\x00\xfe\xff")
UTF16_BOMS = (b"\xff\xfe", b"\xfe\xff")


@dataclass(frozen=True)
class Failure:
    path: str
    line: int | None
    rule: str

    def render(self) -> str:
        where = self.path if self.line is None else f"{self.path}:{self.line}"
        return f"  {where}: {self.rule}"


@dataclass(frozen=True)
class Entry:
    path: str
    mode: str
    sha: str


class GitError(RuntimeError):
    pass


def git(*args: str) -> bytes:
    proc = subprocess.run(["git", *args], capture_output=True, check=False)
    if proc.returncode != 0:
        raise GitError(
            f"git {args[0]} failed: {proc.stderr.decode(errors='replace').strip()}"
        )
    return proc.stdout


def index_entries() -> dict[str, Entry]:
    """Every path in the index (stage 0), with its mode and blob sha."""
    entries: dict[str, Entry] = {}
    for record in git("ls-files", "--stage", "-z").split(b"\0"):
        if not record:
            continue
        meta, _, raw_path = record.partition(b"\t")
        mode, sha, _stage = meta.decode().split()
        path = raw_path.decode("utf-8", errors="surrogateescape")
        entries[path] = Entry(path=path, mode=mode, sha=sha)
    return entries


def staged_paths() -> set[str]:
    out = git(
        "diff", "--cached", "--name-only", "-z", "--diff-filter=ACMRT", "--no-renames"
    )
    return {p.decode("utf-8", errors="surrogateescape") for p in out.split(b"\0") if p}


def read_blobs(shas: list[str]) -> dict[str, bytes]:
    if not shas:
        return {}
    proc = subprocess.run(
        ["git", "cat-file", "--batch"],
        input="".join(f"{sha}\n" for sha in shas).encode(),
        capture_output=True,
        check=False,
    )
    if proc.returncode != 0:
        raise GitError("git cat-file failed")
    blobs: dict[str, bytes] = {}
    out, pos = proc.stdout, 0
    for sha in shas:
        header_end = out.index(b"\n", pos)
        header = out[pos:header_end].split()
        if len(header) != 3:
            raise GitError(f"git cat-file could not read blob {sha}")
        size = int(header[2])
        start = header_end + 1
        blobs[sha] = out[start : start + size]
        pos = start + size + 1
    return blobs


def is_claude_settings(path: str) -> bool:
    p = PurePosixPath(path)
    return p.parent == PurePosixPath(".claude") and p.suffix.lower() == ".json"


def is_statement_type(path: str) -> bool:
    if path.startswith(SYNTHETIC_DIR):
        return False
    suffix = PurePosixPath(path).suffix.lower()
    if suffix == ".json":
        return not path.startswith(REFERENCE_DIR)
    return suffix in STATEMENT_EXTENSIONS


def is_data_shaped(path: str) -> bool:
    if path.startswith(DATA_DIRS):
        return True
    return PurePosixPath(path).suffix.lower() in DATA_EXTENSIONS


def is_calendar_date(run: bytes) -> bool:
    if len(run) != 8 or run[:2] not in (b"19", b"20"):
        return False
    try:
        datetime.date(int(run[:4]), int(run[4:6]), int(run[6:]))
    except ValueError:
        return False
    return True


def is_epoch_ms(run: bytes) -> bool:
    return EPOCH_MS.fullmatch(run) is not None


def luhn_valid(digits: bytes) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = ch - 0x30
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def card_shaped(digits: bytes) -> bool:
    return 13 <= len(digits) <= 19 and not is_epoch_ms(digits) and luhn_valid(digits)


def group_chains(line: bytes) -> Iterator[list[bytes]]:
    """Chains of adjacent groups of up to 6 digits separated by spaces or hyphens."""
    chain: list[bytes] = []
    last_end = -1
    for m in BOUNDED_RUN.finditer(line):
        group = m.group()
        joined = bool(chain) and GROUP_GAP.fullmatch(line[last_end : m.start()])
        if not joined:
            if len(chain) > 1:
                yield chain
            chain = []
        if len(group) <= 6:
            chain.append(group)
            last_end = m.end()
        else:
            if len(chain) > 1:
                yield chain
            chain = []
    if len(chain) > 1:
        yield chain


def grouped_card(chain: list[bytes]) -> bool:
    """True when any window of adjacent groups in a card layout passes Luhn."""
    lengths = tuple(len(group) for group in chain)
    for start in range(len(chain)):
        for layout in CARD_LAYOUTS:
            end = start + len(layout)
            if lengths[start:end] == layout and luhn_valid(b"".join(chain[start:end])):
                return True
    return False


def scan_line(line: bytes) -> Iterator[str]:
    for m in DIGIT_RUN.finditer(line):
        run = m.group()
        if not (
            run.startswith(FAKE_PREFIX) or is_calendar_date(run) or is_epoch_ms(run)
        ):
            yield RULE_DIGITS
        if card_shaped(run):
            yield RULE_CARD
    if any(grouped_card(chain) for chain in group_chains(line)):
        yield RULE_CARD


def utf16_interleave(content: bytes) -> str | None:
    """The UTF-16 codec when every other byte of the sample is NUL, else None."""
    sample = content[:4096]
    half = len(sample) // 2
    if half == 0:
        return None
    even_nuls = sample[0 : 2 * half : 2].count(0)
    odd_nuls = sample[1 : 2 * half : 2].count(0)
    if odd_nuls >= 0.4 * half and even_nuls <= 0.05 * half:
        return "utf-16-le"
    if even_nuls >= 0.4 * half and odd_nuls <= 0.05 * half:
        return "utf-16-be"
    return None


def scannable_bytes(content: bytes) -> bytes | None:
    """Content with ASCII digits adjacent, or None when it cannot be decoded.

    UTF-8, Latin-1 and similar encodings already keep digits as ASCII bytes. Wide
    encodings put NULs between them, so they are decoded first; any other content
    with NUL bytes is refused rather than scanned blind.
    """
    codec: str | None = None
    if content.startswith(UTF32_BOMS):
        codec = "utf-32"
    elif content.startswith(UTF16_BOMS):
        codec = "utf-16"
    elif b"\0" in content:
        codec = utf16_interleave(content)
        if codec is None:
            return None
    if codec is None:
        return content
    try:
        text = content.decode(codec)
    except UnicodeDecodeError:
        return None
    if "\0" in text:
        return None
    return text.encode("utf-8")


def scan_content(path: str, raw: bytes) -> list[Failure]:
    content = scannable_bytes(raw)
    if content is None:
        return [Failure(path, None, RULE_UNSCANNABLE)]
    failures: list[Failure] = []
    for number, line in enumerate(content.split(b"\n"), start=1):
        for rule in dict.fromkeys(scan_line(line)):
            failures.append(Failure(path, number, rule))
    return failures


def manifest_listing(
    entries: dict[str, Entry], blobs: dict[str, bytes]
) -> set[str] | None:
    """Paths the manifest declares synthetic, or None when it does not parse."""
    entry = entries.get(MANIFEST_PATH)
    if entry is None:
        return set()
    try:
        data = tomllib.loads(blobs[entry.sha].decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError):
        return None
    fixtures = data.get("fixture", [])
    if not isinstance(fixtures, list):
        return None
    listed: set[str] = set()
    for item in fixtures:
        if isinstance(item, dict) and item.get("synthetic") is True:
            path = item.get("path")
            if isinstance(path, str):
                listed.add(path)
    return listed


def check(staged_only: bool) -> tuple[list[Failure], int]:
    # From a subdirectory, ls-files lists only that subtree with cwd-relative paths
    # while diff --cached stays root-relative, so every git call runs from the root.
    os.chdir(git("rev-parse", "--show-toplevel").decode().rstrip("\n"))
    entries = index_entries()
    targets = staged_paths() if staged_only else set(entries)
    checked = sorted(p for p in targets if p in entries and not is_claude_settings(p))

    # Regular files only: a symlink's blob is its target path, a submodule has no blob.
    def scannable(path: str) -> bool:
        return entries[path].mode.startswith("100") and is_data_shaped(path)

    wanted = [entries[p].sha for p in checked if scannable(p)]
    if MANIFEST_PATH in entries:
        wanted.append(entries[MANIFEST_PATH].sha)
    blobs = read_blobs(list(dict.fromkeys(wanted)))

    failures: list[Failure] = []
    # The manifest rule runs over the whole index in both modes, so a commit that
    # drops a manifest entry is caught even when the fixture itself is unchanged.
    listed = manifest_listing(entries, blobs)
    if listed is None:
        failures.append(Failure(MANIFEST_PATH, None, RULE_MANIFEST))
    else:
        for path in sorted(entries):
            if (
                path.startswith(SYNTHETIC_DIR)
                and path != MANIFEST_PATH
                and path not in listed
            ):
                failures.append(Failure(path, None, RULE_UNLISTED))

    for path in checked:
        if is_statement_type(path):
            failures.append(Failure(path, None, RULE_STATEMENT))
        if scannable(path):
            failures.extend(scan_content(path, blobs[entries[path].sha]))
    return failures, len(checked)


def plural(n: int, noun: str) -> str:
    return f"{n} {noun}" if n == 1 else f"{n} {noun}s"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="repo_check",
        description="Fail when a tracked or staged file looks like private data.",
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--staged", action="store_true", help="check what the commit will hold"
    )
    mode.add_argument("--tracked", action="store_true", help="check every tracked file")
    args = parser.parse_args(argv)
    scope = "staged" if args.staged else "tracked"

    try:
        failures, count = check(staged_only=args.staged)
    except GitError as exc:
        print(f"repo check could not run: {exc}", file=sys.stderr)
        return 1

    if not failures:
        print(f"repo check ok: {count} {scope} files checked.")
        return 0
    files = len({f.path for f in failures})
    print(
        f"repo check failed: {plural(len(failures), 'problem')}"
        f" in {plural(files, 'file')} ({scope})."
    )
    for failure in failures:
        print(failure.render())
    print(
        "Real data never goes in this repo. Fixtures live under"
        " tests/fixtures/synthetic/, are listed in its MANIFEST.toml, and their digit"
        " runs start with 999."
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
