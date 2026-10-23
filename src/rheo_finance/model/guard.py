"""The prompt guard (FR-47; SPEC "Privacy and safety mechanics" > "Prompt guard").

Every model call passes through ``PromptGuard.check`` before anything is sent, the
stub included (``client.GuardedModelClient.call`` is the one caller). The guard
REFUSES a prompt; it never masks one, so what a model sees is exactly what a caller
built, or nothing. A prompt is refused when it holds:

- a run of 7 or more digits (``digit_run``);
- a 13 to 19 digit card shape: contiguous, in groups of four, or grouped 4-6-5
  (``card_shape``);
- a phone shape: a North American number with separators, a ``+`` international
  number, or a hyphenated local number (``phone_shape``);
- a registered account identifier, a party alias, an own or alias address, a TXID on
  file, or the account holder's name (``identifier``, ``party_alias``, ``address``,
  ``txid``, ``owner_name``): terms a ``TermsProvider`` supplies at check time, so later
  prompts add providers instead of editing the guard;
- an unbroken run of 26 or more letters and digits (``long_token``);
- a binary, PDF or image signature, raw or base64 (``binary``, ``pdf``, ``image``).

Text is checked after NFKC normalization with format characters (zero-width spaces
and joiners) removed, so a lookalike digit or an invisible break does not slip a run
through. A term matches as whole words, case-insensitively, or, when it holds 6 or
more letters and digits, anywhere once separators are dropped on both sides. An owner
name also matches by each of its parts of 3 or more letters.

A refusal names its class and never the text that tripped it: refusals are counted
and printed, and the text is what must not travel. A provider that cannot read its
terms refuses every prompt (``terms_unavailable``): the guard fails closed.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal

from rheo_finance.manual.accounts import (
    ACCOUNTS_FILE,
    IDENTIFIERS_FILE,
    AccountIdentifierRow,
    AccountRow,
)
from rheo_finance.manual.headers import MANUAL_DIR_NAME
from rheo_finance.manual.loader import load_csv

RefusalKind = Literal[
    "binary",
    "pdf",
    "image",
    "digit_run",
    "card_shape",
    "phone_shape",
    "long_token",
    "identifier",
    "party_alias",
    "address",
    "txid",
    "owner_name",
    "terms_unavailable",
]


class ModelError(Exception):
    """A model step did not produce a reply: refused, unavailable or invalid."""


class PromptRefused(ModelError):
    """The guard refused a prompt. Nothing was sent."""

    def __init__(self, kind: RefusalKind) -> None:
        super().__init__(f"the prompt guard refused the prompt: {kind}")
        self.kind: RefusalKind = kind


class TermsUnavailable(Exception):
    """A provider could not read its terms; the guard then refuses every prompt."""


@dataclass(frozen=True)
class ProtectedTerms:
    """Strings no prompt may hold, by refusal class."""

    identifiers: tuple[str, ...] = ()
    party_aliases: tuple[str, ...] = ()
    addresses: tuple[str, ...] = ()
    txids: tuple[str, ...] = ()
    owner_names: tuple[str, ...] = ()


TermsProvider = Callable[[], ProtectedTerms]

# Raw signatures, looked for before normalization. The replacement character marks
# bytes that were decoded as text but were not text.
_BINARY: Final = re.compile(
    r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\N{REPLACEMENT CHARACTER}]"
)
_PDF: Final = re.compile(r"%PDF-|JVBERi0")
_IMAGE: Final = re.compile(
    r"\x89PNG|\xff\xd8\xff|GIF8[79]a|RIFF.{4}WEBP|ftyp(?:heic|heix|avif|mif1)"
    r"|data:image/|iVBORw0KGgo|R0lGOD[dl]h|UklGR|(?<![A-Za-z0-9+/])/9j/",
    re.DOTALL,
)
# Shapes, looked for in the normalized text.
_SHAPES: Final[tuple[tuple[RefusalKind, re.Pattern[str]], ...]] = (
    ("card_shape", re.compile(
        r"(?<!\d)(?:\d{13,19}|(?:\d{4}[ -]){3}\d{1,7}|\d{4}[ -]\d{6}[ -]\d{4,5})(?!\d)"
    )),
    ("digit_run", re.compile(r"\d{7,}")),
    ("phone_shape", re.compile(
        r"(?<![\d+])(?:\+?1[ .-]?)?(?:\(\d{3}\)[ .-]?|\d{3}[ .-])\d{3}[ .-]\d{4}(?!\d)"
        r"|\+\d{1,3}(?:[ .()-]?\d){6,14}(?!\d)"
        r"|(?<![\d.-])\d{3}-\d{4}(?![\d-])"
    )),
    ("long_token", re.compile(r"[^\W_]{26,}")),
)  # fmt: skip
_MIN_COMPACT: Final = 6
_MIN_NAME_PART: Final = 3


def _normalized(text: str) -> str:
    folded = unicodedata.normalize("NFKC", text)
    return "".join(ch for ch in folded if unicodedata.category(ch) != "Cf")


def _spaced(text: str) -> str:
    return " ".join(_normalized(text).casefold().split())


def _compact(text: str) -> str:
    return "".join(ch for ch in _normalized(text).casefold() if ch.isalnum())


def holds_image(text: str) -> bool:
    """Whether ``text`` carries an image signature, raw or base64."""
    return _IMAGE.search(text) is not None


@dataclass(frozen=True)
class _Prepared:
    spaced: str
    compact: str

    def names(self, term: str) -> bool:
        spaced = _spaced(term)
        if not spaced:
            return False
        if re.search(rf"(?<!\w){re.escape(spaced)}(?!\w)", self.spaced):
            return True
        compact = _compact(term)
        return len(compact) >= _MIN_COMPACT and compact in self.compact


def _name_terms(names: Iterable[str]) -> list[str]:
    """Each name, and each of its parts of 3 or more letters."""
    found: list[str] = []
    for name in names:
        found.append(name)
        found.extend(
            part
            for part in name.split()
            if sum(ch.isalpha() for ch in part) >= _MIN_NAME_PART
        )
    return found


class PromptGuard:
    """The single choke point every prompt passes before a model sees it."""

    def __init__(self, providers: Sequence[TermsProvider] = ()) -> None:
        self._providers = tuple(providers)

    def refusal(self, *texts: str) -> RefusalKind | None:
        """The class that refuses ``texts`` (the parts of one prompt), or None."""
        for text in texts:
            if _BINARY.search(text):
                return "binary"
            if _PDF.search(text):
                return "pdf"
            if holds_image(text):
                return "image"
            normalized = _normalized(text)
            for kind, pattern in _SHAPES:
                if pattern.search(normalized):
                    return kind
        prepared = [_Prepared(_spaced(text), _compact(text)) for text in texts]
        for provider in self._providers:
            try:
                terms = provider()
            except TermsUnavailable:
                return "terms_unavailable"
            by_kind: tuple[tuple[RefusalKind, Iterable[str]], ...] = (
                ("identifier", terms.identifiers),
                ("party_alias", terms.party_aliases),
                ("address", terms.addresses),
                ("txid", terms.txids),
                ("owner_name", _name_terms(terms.owner_names)),
            )
            for kind, values in by_kind:
                for value in values:
                    if any(each.names(value) for each in prepared):
                        return kind
        return None

    def check(self, *texts: str) -> None:
        """Raise ``PromptRefused`` when any of ``texts`` is refused."""
        kind = self.refusal(*texts)
        if kind is not None:
            raise PromptRefused(kind)


def manual_accounts_terms(data_dir: Path) -> TermsProvider:
    """The registered identifiers and the account holders' names (the ``owner``
    column) from the manual files under ``data_dir``, read on first use.

    Every row that loads counts, whatever the registry rules later reject. A file
    with a row that does not load (its values unknown here) makes the provider raise
    ``TermsUnavailable``, so the guard refuses until the file is fixed.
    shortcut: read once per provider; a context lives one command or job tick, so a
    file edited mid-run is seen by the next one.
    """
    loaded: list[ProtectedTerms] = []

    def provide() -> ProtectedTerms:
        if not loaded:
            manual = data_dir / MANUAL_DIR_NAME
            accounts = load_csv(manual / ACCOUNTS_FILE, AccountRow)
            identifiers = load_csv(manual / IDENTIFIERS_FILE, AccountIdentifierRow)
            if accounts.errors or identifiers.errors:
                raise TermsUnavailable("a manual accounts file has a row that fails")
            loaded.append(
                ProtectedTerms(
                    identifiers=tuple(
                        row.identifier
                        for row in identifiers.rows
                        if row.kind != "internal"
                    ),
                    owner_names=tuple(row.owner for row in accounts.rows if row.owner),
                )
            )
        return loaded[0]

    return provide
