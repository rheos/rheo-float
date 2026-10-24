"""Issuer description patterns: what a line's wording says it is (FR-15, FR-15(a),
FR-15(c); SPEC Derivation stage 9).

``classify(description_norm, *, account_kind, issuer)`` reads the wording of one line
against ``patterns.json`` and returns its ``Hints``. Every parser that sets the hint
columns of ``document_line`` calls it with the line's ``description_norm``, its
account's kind and its document's issuer key; a parser never passes ``hint_rows``, so
a line's parse output depends on the repo's patterns only, never on the owner's files.

How a wording matches: as a whole-token run of the normalized text, so ``return`` does
not match inside ``returns`` and ``nsf`` not inside ``transfer``. An entry matches when
every part of its ``all`` list matches, no part of its ``unless`` list does, and the
line's issuer and account kind are in its ``issuers`` and ``account_kinds`` (when it
names them). In each section the first matching entry in file order wins.

The fields:

- ``transfer_hint``: the transfer kind (``card_payment``, ``paypal_preauthorized``,
  ``stripe_payout``; ``manual_out`` or ``manual_in`` for a ``transfer_hints.csv``
  row). A bill payment to a vendor names no own account and gets none: it is spending.
- ``hint_account_slug``: the counterpart the wording names. For a built-in pattern it
  is an issuer key of the archive's issuer map (``documents.issuers.ISSUER_DIRS``),
  which stage 9 resolves to the registered account(s) of that issuer; for a
  ``manual_*`` hint it is the owner's own account slug from the row.
- ``reversal_hint``: ``out`` when the wording says the owner's own payment was
  cancelled (the reversal is a credit), ``in`` when a received payment was clawed back
  (the reversal is a debit). Stage 8 pairs by the line's sign, so the direction is the
  wording's claim, never what pairing relies on. A wording that names no direction
  ("reversed", "returned") has direction ``sign`` in the file: ``credit`` settles it as
  a ``reversal`` row's does (a credit is ``out``, a debit ``in``), and with ``credit``
  None it sets nothing.
- ``conversion_hint``: the wording says one balance was converted into another
  currency on the same account.
- ``etransfer_hint`` and ``etransfer_name_position``: the e-Transfer wording matched,
  and where the name it introduces sits: ``after`` (the text after the wording) or
  ``parenthesized`` (the first parenthesized group after it); None when the wording
  prints no name. ``etransfer_name`` reads that text.
- ``fee_type``: the kind of fee the wording names.

``hint_rows`` (from ``manual.transfer_hints``) are consulted after the built-in
patterns, in file order, and only fill what the built-ins left empty: an ``out`` or
``in`` row sets ``transfer_hint`` and its counterpart, a ``conversion`` row sets
``conversion_hint``, and a ``reversal`` row sets ``reversal_hint`` from ``credit``
(the line's sign, which the row does not carry), so with ``credit`` None a reversal
row sets nothing.
"""

from __future__ import annotations

import functools
import hashlib
import importlib.resources
import re
from collections.abc import Sequence
from typing import Annotated, Final, Literal, Self

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    model_validator,
)

from rheo_finance.documents.issuers import ISSUER_DIRS
from rheo_finance.manual.accounts import AccountKind
from rheo_finance.manual.transfer_hints import TransferHintRow
from rheo_finance.text import normalize_description

PATTERNS_FILE: Final = "patterns.json"

ReversalDirection = Literal["out", "in"]
NamePosition = Literal["after", "parenthesized"]


class Hints(BaseModel):
    """What one line's wording says it is. The default is a line with no hint."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    transfer_hint: str | None = None
    reversal_hint: ReversalDirection | None = None
    conversion_hint: bool = False
    etransfer_hint: str | None = None
    etransfer_name_position: NamePosition | None = None
    hint_account_slug: str | None = None
    fee_type: str | None = None


# --- the patterns file ---------------------------------------------------------------


def _wording(value: str) -> str:
    normalized = normalize_description(value)
    if not normalized:
        raise ValueError("a wording is blank")
    if normalized != value:
        raise ValueError(f"a wording is not written normalized: {value!r}")
    return value


Wording = Annotated[str, AfterValidator(_wording)]


def _issuer_key(value: str) -> str:
    if value not in ISSUER_DIRS:
        raise ValueError(f"not an issuer key of the archive's issuer map: {value!r}")
    return value


IssuerKey = Annotated[str, AfterValidator(_issuer_key)]


class _Scoped(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)

    parts: tuple[Wording, ...] = Field(alias="all", min_length=1)
    unless: tuple[Wording, ...] = ()
    issuers: tuple[IssuerKey, ...] | None = None
    account_kinds: tuple[AccountKind, ...] | None = None
    note: str | None = None


class TransferPattern(_Scoped):
    kind: Annotated[str, Field(pattern=r"^[a-z][a-z_]*$")]
    counterpart: IssuerKey | None = None

    @model_validator(mode="after")
    def _not_a_manual_kind(self) -> Self:
        if self.kind.startswith("manual_"):
            raise ValueError("the manual_ kinds belong to transfer_hints.csv rows")
        return self


class ReversalPattern(_Scoped):
    # ``sign``: the wording names no direction; the line's sign settles it.
    direction: ReversalDirection | Literal["sign"]


class ConversionPattern(_Scoped):
    pass


class FeePattern(_Scoped):
    fee_type: Annotated[str, Field(pattern=r"^[a-z][a-z_]*$")]


class ETransferPattern(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    wording: Wording
    name: Literal["after", "parenthesized", "none"]
    unless: tuple[Wording, ...] = ()
    issuers: tuple[IssuerKey, ...] | None = None
    account_kinds: tuple[AccountKind, ...] | None = None
    note: str | None = None

    @property
    def parts(self) -> tuple[str, ...]:
        return (self.wording,)


class PatternFile(BaseModel):
    """``patterns.json``, validated: every wording normalized, every issuer and
    counterpart an issuer key, every account kind a registry kind."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    about: tuple[str, ...]
    transfer: tuple[TransferPattern, ...]
    reversal: tuple[ReversalPattern, ...]
    conversion: tuple[ConversionPattern, ...]
    fee: tuple[FeePattern, ...]
    etransfer: tuple[ETransferPattern, ...]


def _patterns_bytes() -> bytes:
    return importlib.resources.files(__package__).joinpath(PATTERNS_FILE).read_bytes()


@functools.cache
def pattern_file() -> PatternFile:
    """The repo's ``patterns.json``, read and validated once."""
    return PatternFile.model_validate_json(_patterns_bytes().decode("utf-8"))


@functools.cache
def patterns_digest() -> str:
    """sha256 of ``patterns.json``'s bytes. A hint-setting parser's output depends on
    it, so ``derive.stage1.parse_inputs`` and the input fingerprint hash it."""
    return hashlib.sha256(_patterns_bytes()).hexdigest()


# --- matching ------------------------------------------------------------------------


@functools.lru_cache(maxsize=1024)
def _token_run(wording: str) -> re.Pattern[str]:
    """``wording`` as a whole-token run: no word character right before it when it
    starts with one, none right after it when it ends with one."""
    head = r"(?<!\w)" if re.match(r"\w", wording) else ""
    tail = r"(?!\w)" if re.search(r"\w$", wording) else ""
    return re.compile(head + re.escape(wording) + tail)


def _has(text: str, wording: str) -> bool:
    return _token_run(wording).search(text) is not None


def _applies[E: _Scoped | ETransferPattern](
    entries: Sequence[E], text: str, account_kind: str, issuer: str
) -> E | None:
    for entry in entries:
        if entry.issuers is not None and issuer not in entry.issuers:
            continue
        if entry.account_kinds is not None and account_kind not in entry.account_kinds:
            continue
        if all(_has(text, part) for part in entry.parts) and not any(
            _has(text, part) for part in entry.unless
        ):
            return entry
    return None


def classify(
    description_norm: str,
    *,
    account_kind: str,
    issuer: str,
    hint_rows: Sequence[TransferHintRow] = (),
    credit: bool | None = None,
) -> Hints:
    """The hints one line's wording gives (see the module docstring)."""
    patterns = pattern_file()
    text = description_norm
    transfer = _applies(patterns.transfer, text, account_kind, issuer)
    reversal = _applies(patterns.reversal, text, account_kind, issuer)
    conversion = _applies(patterns.conversion, text, account_kind, issuer)
    fee = _applies(patterns.fee, text, account_kind, issuer)
    etransfer = _applies(patterns.etransfer, text, account_kind, issuer)

    rows = row_hints(text, hint_rows, credit=credit)
    reversal_hint: ReversalDirection | None = rows.reversal_hint
    if reversal is not None:
        reversal_hint = (
            _by_sign(credit) if reversal.direction == "sign" else reversal.direction
        )

    return Hints(
        transfer_hint=transfer.kind if transfer else rows.transfer_hint,
        reversal_hint=reversal_hint,
        conversion_hint=conversion is not None or rows.conversion_hint,
        etransfer_hint=etransfer.wording if etransfer else None,
        etransfer_name_position=(
            None if etransfer is None or etransfer.name == "none" else etransfer.name
        ),
        hint_account_slug=transfer.counterpart if transfer else rows.hint_account_slug,
        fee_type=fee.fee_type if fee else None,
    )


def _by_sign(credit: bool | None) -> ReversalDirection | None:
    return None if credit is None else "out" if credit else "in"


def row_hints(
    description_norm: str,
    hint_rows: Sequence[TransferHintRow],
    *,
    credit: bool | None = None,
) -> Hints:
    """The hints ``transfer_hints.csv`` rows alone give one line's wording, as
    ``classify`` applies them after its built-in patterns: the first matching ``out``
    or ``in`` row sets ``transfer_hint`` (``manual_out`` or ``manual_in``) and its
    counterpart account slug, a ``conversion`` row sets ``conversion_hint``, and a
    ``reversal`` row sets ``reversal_hint`` from ``credit``. Parsers never pass the
    owner's rows, so derive stage 9 asks this for every line beside its parser hints."""
    transfer_hint: str | None = None
    counterpart: str | None = None
    conversion_hint = False
    reversal_hint: ReversalDirection | None = None
    for row in hint_rows:
        if not _has(description_norm, row.pattern):
            continue
        if row.direction in ("out", "in"):
            if transfer_hint is None:
                transfer_hint = f"manual_{row.direction}"
                counterpart = row.counterpart_account
        elif row.direction == "conversion":
            conversion_hint = True
        elif reversal_hint is None:
            reversal_hint = _by_sign(credit)
    return Hints(
        transfer_hint=transfer_hint,
        reversal_hint=reversal_hint,
        conversion_hint=conversion_hint,
        hint_account_slug=counterpart,
    )


def reversal_row_matches(
    description_norm: str, hint_rows: Sequence[TransferHintRow]
) -> bool:
    """Whether a ``reversal`` row of ``transfer_hints.csv`` matches a line's wording,
    as ``classify`` matches its rows. Parsers never pass the owner's rows, so derive
    stage 8 asks this for every line beside the parser's ``reversal_hint``."""
    return any(
        row.direction == "reversal" and _has(description_norm, row.pattern)
        for row in hint_rows
    )


_PARENTHESIZED: Final = re.compile(r"\(([^()]*)\)")


def etransfer_name(description_norm: str, hints: Hints) -> str | None:
    """The text at the name position of an e-Transfer line, or None.

    A printed name: restricted, never stored or printed from here. Stage 7 matches
    ``payees.csv`` patterns against it and strips it from the merchant key.
    """
    if hints.etransfer_hint is None or hints.etransfer_name_position is None:
        return None
    match = _token_run(hints.etransfer_hint).search(description_norm)
    if match is None:
        return None
    rest = description_norm[match.end() :]
    if hints.etransfer_name_position == "parenthesized":
        group = _PARENTHESIZED.search(rest)
        rest = group.group(1) if group else ""
    name = rest.strip(" -:")
    return name or None
