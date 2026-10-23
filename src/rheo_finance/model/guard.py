"""The prompt guard (FR-47; SPEC "Privacy and safety mechanics" > "Prompt guard").

Every model call passes through ``PromptGuard.check`` before anything is sent, the
stub included (``client.GuardedModelClient.call`` is the one caller). The guard
REFUSES a prompt; it never masks one, so what a model sees is exactly what a caller
built, or nothing. A prompt is refused when it holds:

- a phone shape: a North American number with separators, a ``+`` international
  number, or a hyphenated local number (``phone_shape``);
- a 13 to 19 digit card shape (``card_shape``) or any other number of 7 or more
  digits (``digit_run``). Digits joined by runs of whitespace, dots, slashes,
  hyphens or Unicode dashes are one number whatever the grouping (4-4-4-4, 3-3-3,
  two spaces as a layout extract prints them, one digit at a time, across a line
  break), and so is a comma-grouped integer. An ISO date with a real month and day
  and a money amount with cents (comma-grouped, or at most six digits before the
  point) are admitted first, so records of dates and amounts pass;
- a registered account identifier, a party alias, an own or alias address, a TXID on
  file, or the account holder's name (``identifier``, ``party_alias``, ``address``,
  ``txid``, ``owner_name``): terms a ``TermsProvider`` supplies at check time, so later
  prompts add providers instead of editing the guard;
- an unbroken run of 26 or more letters and digits (``long_token``);
- a binary, PDF or image signature, raw or base64 (``binary``, ``pdf``, ``image``),
  looked for both as given and normalized.

Text is checked after NFKC normalization with every format character and every
default-ignorable code point (zero-width spaces and joiners, variation selectors,
fillers, tag characters) removed, so a fullwidth digit or an invisible break does
not slip a run through. Terms are compared after that, decomposed with combining
marks dropped and casefolded on both sides, so an accented or fullwidth spelling
still matches. A term matches as whole words, or, when it holds 6 or more letters
and digits, anywhere once separators are dropped on both sides. An owner name also
matches by each of its parts of 3 or more letters. Cross-script lookalikes (a
Cyrillic letter for a Latin one) are not folded.

The system prompt and the text are checked alone and joined, so a number, a
signature or a term split across the two is refused too.

A refusal names its class and never the text that tripped it: refusals are counted
and printed, and the text is what must not travel. A provider that cannot read its
terms (a file missing, unreadable or with a row that does not load) refuses every
prompt (``terms_unavailable``): the guard fails closed.
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

# Raw signatures, looked for before normalization. The replacement character and a
# lone surrogate mark bytes that were decoded as text but were not text (a surrogate
# also cannot be encoded as UTF-8 to be sent).
_SURROGATES: Final = f"{chr(0xD800)}-{chr(0xDFFF)}"
_BINARY: Final = re.compile(
    rf"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\N{{REPLACEMENT CHARACTER}}{_SURROGATES}]"
)
_PDF: Final = re.compile(r"%PDF-|JVBERi0")
_IMAGE: Final = re.compile(
    r"\x89PNG|\xff\xd8\xff|GIF8[79]a|RIFF.{4}WEBP|ftyp(?:heic|heix|avif|mif1)"
    r"|data:image/|iVBORw0KGgo|R0lGOD[dl]h|UklGR|(?<![A-Za-z0-9+/])/9j/",
    re.DOTALL,
)
# Admitted before digits are counted: an ISO date (a real month and day), and a
# money amount with cents (comma-grouped, or at most six digits before the point).
_ADMITTED: Final = re.compile(
    r"(?<!\d)\d{4}([-/.])(?:0[1-9]|1[0-2])\1(?:0[1-9]|[12]\d|3[01])(?!\d)"
    r"|(?<![\d.,])-?\d{1,3}(?:,\d{3})+\.\d{2}(?!\d)"
    r"|(?<![\d.,])\d{1,6}\.\d{2}(?!\d)"
)
_PHONE: Final = re.compile(
    r"(?<![\d+])(?:\+?1[ .-]?)?(?:\(\d{3}\)[ .-]?|\d{3}[ .-])\d{3}[ .-]\d{4}(?!\d)"
    r"|\+\d{1,3}(?:[ .()-]?\d){6,14}(?!\d)"
    r"|(?<![\d.-])\d{3}-\d{4}(?![\d-])"
)
# Digits joined by runs of whitespace (tabs and newlines too), dots, slashes,
# hyphens or Unicode dashes (U+2010 to U+2015, U+2212) count as one number, however
# they are grouped: 4-4-4-4, 3-3-3, two spaces apart, one digit at a time. A comma
# before exactly three digits joins too, so a comma-grouped integer is one number.
_DASHES: Final = "".join(re.escape(chr(c)) for c in (*range(0x2010, 0x2016), 0x2212))
_DIGITS: Final = re.compile(rf"\d+(?:(?:[\s./\-{_DASHES}]+|,(?=\d{{3}}(?!\d)))\d+)*")
_CARD_DIGITS: Final = range(13, 20)
_RUN_DIGITS: Final = 7
_LONG_TOKEN: Final = re.compile(r"[^\W_]{26,}")
_MIN_COMPACT: Final = 6
_MIN_NAME_PART: Final = 3
# Unicode's Default_Ignorable_Code_Point set (DerivedCoreProperties), as ranges.
_IGNORABLE_RANGES: Final = (
    (0x00AD, 0x00AD), (0x034F, 0x034F), (0x061C, 0x061C), (0x115F, 0x1160),
    (0x17B4, 0x17B5), (0x180B, 0x180F), (0x200B, 0x200F), (0x202A, 0x202E),
    (0x2060, 0x206F), (0x3164, 0x3164), (0xFE00, 0xFE0F), (0xFEFF, 0xFEFF),
    (0xFFA0, 0xFFA0), (0xFFF0, 0xFFF8), (0x1BCA0, 0x1BCA3), (0x1D173, 0x1D17A),
    (0xE0000, 0xE0FFF),
)  # fmt: skip
_IGNORABLE: Final = re.compile(
    "["
    + "".join(
        f"{re.escape(chr(low))}-{re.escape(chr(high))}"
        for low, high in _IGNORABLE_RANGES
    )
    + "]"
)


def _normalized(text: str) -> str:
    """NFKC, then every format character and default-ignorable code point removed."""
    folded = _IGNORABLE.sub("", unicodedata.normalize("NFKC", text))
    return "".join(ch for ch in folded if unicodedata.category(ch) != "Cf")


def _folded(text: str) -> str:
    """``_normalized``, decomposed with every combining mark dropped, casefolded:
    the form terms are compared in, so an accent does not hide a name."""
    decomposed = unicodedata.normalize("NFD", _normalized(text))
    return "".join(
        ch for ch in decomposed if not unicodedata.category(ch).startswith("M")
    ).casefold()


def _spaced(text: str) -> str:
    return " ".join(_folded(text).split())


def _compact(text: str) -> str:
    return "".join(ch for ch in _folded(text) if ch.isalnum())


def _shape(normalized: str) -> RefusalKind | None:
    """The shape class ``normalized`` holds, dates and amounts admitted first."""
    masked = _ADMITTED.sub(";", normalized)
    if _PHONE.search(masked):
        return "phone_shape"
    for found in _DIGITS.finditer(masked):
        count = sum(ch.isdigit() for ch in found.group())
        if count in _CARD_DIGITS:
            return "card_shape"
        if count >= _RUN_DIGITS:
            return "digit_run"
    if _LONG_TOKEN.search(normalized):
        return "long_token"
    return None


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
        """The class that refuses ``texts`` (the parts of one prompt), or None.

        Each part is checked alone and the parts joined, so nothing split across
        the system prompt and the text gets through."""
        raw = [*texts, "".join(texts)] if len(texts) > 1 else list(texts)
        # Signatures as given and normalized, so an invisible break inside one (or
        # a fullwidth spelling) does not hide it.
        for text in [*raw, *(_normalized(each) for each in raw)]:
            if _BINARY.search(text):
                return "binary"
            if _PDF.search(text):
                return "pdf"
            if holds_image(text):
                return "image"
        parts = [*texts, " ".join(texts)] if len(texts) > 1 else list(texts)
        for text in parts:
            kind = _shape(_normalized(text))
            if kind is not None:
                return kind
        prepared = [_Prepared(_spaced(text), _compact(text)) for text in parts]
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

    Every row that loads counts, whatever the registry rules later reject. The
    provider raises ``TermsUnavailable``, so the guard refuses until it is fixed,
    when either file is missing or cannot be read, or has a row that does not load
    (its values unknown here). ``finance db init`` creates both files.
    shortcut: read once per provider; a context lives one command or job tick, so a
    file edited mid-run is seen by the next one.
    """
    loaded: list[ProtectedTerms] = []

    def provide() -> ProtectedTerms:
        if not loaded:
            manual = data_dir / MANUAL_DIR_NAME
            files = (manual / ACCOUNTS_FILE, manual / IDENTIFIERS_FILE)
            try:
                if not all(path.is_file() for path in files):
                    raise TermsUnavailable("a manual accounts file is missing")
                accounts = load_csv(files[0], AccountRow)
                identifiers = load_csv(files[1], AccountIdentifierRow)
            except OSError:
                raise TermsUnavailable(
                    "a manual accounts file cannot be read"
                ) from None
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
