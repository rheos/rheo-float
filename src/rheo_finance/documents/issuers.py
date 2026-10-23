"""The archive's directory-to-issuer map, and the kind and format of a file in it.

The map is code, not settings (SPEC "Files, paths and settings" > Archive): each
top-level directory of ``finance.paths.archive`` names one issuer, and the parser
registry picks parsers by that issuer. Two directories are nested: in
``invoices-received/`` and ``receipts/`` each sub-directory is its own issuer
(``invoices-received/<vendor>``). A file directly under a nested directory has the
directory itself as its issuer.

A file's kind comes from its directory, then its format, then its name:

- an image (jpg, jpeg, png, heic) is kind ``image`` wherever it sits: no image is ever
  machine-read, so the manual-entry queue is its only way in (FR-9);
- a file under an ``api/`` sub-directory of a non-nested issuer is an API response
  (``<issuer-dir>/api/<date>-<endpoint>.json``);
- in ``coinstats/``, ``portfolio-coins-*.json`` is a holdings snapshot and
  ``transactions-*.json`` a history (an export);
- otherwise the directory's own rule, with its per-format exceptions (a CSV in a
  statement issuer's directory is an export).

A path whose top-level directory is not in the map has no issuer: the walk counts it
as ``unmapped_directory`` and registers nothing.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from fnmatch import fnmatchcase
from pathlib import PurePosixPath
from types import MappingProxyType
from typing import Final, Literal

DocumentKind = Literal[
    "statement", "bill", "export", "trading_export", "holdings_snapshot", "invoice",
    "receipt", "email", "drop_record", "api_response", "image", "cra_slip",
]  # fmt: skip
DOCUMENT_KINDS: Final[tuple[DocumentKind, ...]] = (
    "statement", "bill", "export", "trading_export", "holdings_snapshot", "invoice",
    "receipt", "email", "drop_record", "api_response", "image", "cra_slip",
)  # fmt: skip

IMAGE_FORMATS: Final = frozenset({"jpg", "jpeg", "png", "heic"})
# The format of a file with no extension.
NO_FORMAT: Final = "none"
API_DIR: Final = "api"
# The skip-count directory of a path outside every mapped directory: its own top-level
# name is free text that may carry an identifier, so it is never recorded.
UNMAPPED: Final = "(unmapped)"

_TABULAR: Final[Mapping[str, DocumentKind]] = MappingProxyType(
    {"csv": "export", "json": "export", "xlsx": "export", "xls": "export"}
)


@dataclass(frozen=True)
class IssuerDir:
    """One top-level archive directory: its default kind, the kinds some formats and
    file names take instead, and whether each sub-directory is its own issuer."""

    kind: DocumentKind
    by_format: Mapping[str, DocumentKind] = field(
        default_factory=lambda: MappingProxyType({})
    )
    # (glob over the file name, kind), first match wins; checked before by_format.
    by_name: tuple[tuple[str, DocumentKind], ...] = ()
    nested: bool = False


def _statements() -> IssuerDir:
    return IssuerDir("statement", _TABULAR)


def _bills() -> IssuerDir:
    return IssuerDir("bill", _TABULAR)


ISSUER_DIRS: Final[Mapping[str, IssuerDir]] = MappingProxyType(
    {
        "vancity": _statements(),
        "vancity-visa": _statements(),
        "mbna": _statements(),
        "wise": _statements(),
        "nelson-district-cu": _statements(),
        "paypal": _statements(),
        "virgin-plus": _bills(),
        "fortisbc": _bills(),
        "coinstats": IssuerDir(
            "export",
            by_name=(
                ("portfolio-coins-*.json", "holdings_snapshot"),
                ("transactions-*.json", "export"),
            ),
        ),
        "blofin": IssuerDir("trading_export"),
        "invoices-received": IssuerDir("invoice", nested=True),
        "receipts": IssuerDir("receipt", nested=True),
        "_mail": IssuerDir("email"),
        "_drops": IssuerDir("drop_record"),
        "cra": IssuerDir("cra_slip"),
        # Connector directories: what the vendor APIs return, plus any CSV export the
        # owner downloads from the same vendor.
        "kraken": IssuerDir("api_response", {**_TABULAR, "json": "api_response"}),
        "stripe": IssuerDir("api_response", {**_TABULAR, "json": "api_response"}),
    }
)


@dataclass(frozen=True)
class Placement:
    """What the archive map says about one archive-relative file path."""

    issuer: str
    kind: DocumentKind
    format: str


def file_format(name: str) -> str:
    """The lower-cased extension of ``name`` without its dot, or ``none``."""
    suffix = PurePosixPath(name).suffix.lower().removeprefix(".")
    return suffix if suffix.isalnum() and suffix.isascii() else NO_FORMAT


def skip_directory(relative: PurePosixPath) -> str:
    """The directory a skipped file is counted under: its mapped top-level directory,
    or ``(unmapped)``. Never a sub-directory, which is free text."""
    if len(relative.parts) > 1 and relative.parts[0] in ISSUER_DIRS:
        return relative.parts[0]
    return UNMAPPED


def place(relative: PurePosixPath) -> Placement | None:
    """The issuer, kind and format of the file at archive-relative ``relative``, or
    None when its top-level directory is not in the map."""
    parts = relative.parts
    if len(parts) < 2 or parts[0] not in ISSUER_DIRS:
        return None
    top = parts[0]
    rule = ISSUER_DIRS[top]
    issuer = f"{top}/{parts[1]}" if rule.nested and len(parts) > 2 else top
    fmt = file_format(relative.name)
    return Placement(issuer, _kind(rule, parts, fmt), fmt)


def _kind(rule: IssuerDir, parts: tuple[str, ...], fmt: str) -> DocumentKind:
    if fmt in IMAGE_FORMATS:
        return "image"
    if not rule.nested and len(parts) > 2 and parts[1] == API_DIR:
        return "api_response"
    name = parts[-1]
    for pattern, kind in rule.by_name:
        if fnmatchcase(name, pattern):
            return kind
    return rule.by_format.get(fmt, rule.kind)
