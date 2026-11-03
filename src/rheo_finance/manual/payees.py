"""The payees: ``payees.csv``, the account-backed payees of ``accounts.csv``, and the
vendor payees of the bill issuers (FR-10, FR-46; SPEC "Manual-entry files", Data
Models ``payee``).

``PayeeRow`` validates one ``payees.csv`` row: a slug and a label (the owner's own
words), a kind from {vendor, person, account}, the printed-name patterns that mean
this payee (``;``-separated, held normalized as a line's ``description_norm`` is, and
matched folded, ``compared``, as a whole-token run: ``Sample-ISP`` names ``SAMPLE
ISP 99901``), an optional party slug and an optional ``paid_from``
(the account slugs the owner pays this payee from, which stage 14's presumed-paid test
reads in place of the derived paying accounts). A label or slug that, compared
(``compared``: casefolded, every run of punctuation and whitespace one space), equals
one of the row's own patterns is an error: a printed name never becomes a label by
accident (FR-46).

``build_payees`` then makes the whole set, in this order, and records every failure
on the row involved:

1. **Account-backed payees.** Every loaded account with ``payee_patterns`` is a payee
   of kind ``account``: its slug, its label and those patterns.
2. **``payees.csv`` rows.** Refused: a slug on two rows (every row of it); a slug an
   account-backed payee already holds; a row of kind ``account`` whose slug is not a
   loaded account's (the account it stands for is the one of its slug); a
   ``paid_from`` slug no loaded account has; a slug or label the adopted-path check
   refuses (``path_check.PathCheck``: a run of 8 or more digits that is not a date, or
   a registered identifier), since the report views show both.
3. **Bill issuers.** Each issuer key whose archive directory holds bills
   (``documents.issuers.ISSUER_DIRS`` kind ``bill``) that no payee above holds is a
   ``vendor`` payee of that slug, labelled with it, with no patterns, so a parsed bill
   always has its payee.

Stage 2 stores every payee in ``payee`` (the patterns restricted) and holds the set in
memory: stage 3 reads the patterns, stage 14 the ``paid_from`` accounts. The party
slug is stored as the payee's ``party_ref`` text as written; nothing resolves it
until the peer-money feature defines parties.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Annotated, Final, Literal, Self

from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    model_validator,
)

from rheo_finance.documents import issuers
from rheo_finance.manual.accounts import (
    ACCOUNTS_FILE,
    CARRIES_IDENTIFIER,
    Registry,
    Slug,
    Text,
)
from rheo_finance.manual.loader import (
    LoadResult,
    RowError,
    duplicate_lines,
    split_list,
)
from rheo_finance.path_check import PathCheck
from rheo_finance.text import normalize_description

PAYEES_FILE: Final = "payees.csv"

PayeeKind = Literal["vendor", "person", "account"]

_NOT_WORD: Final = re.compile(r"[\W_]+")


def compared(text: str) -> str:
    """``text`` folded as a party alias is (FR-57): normalized as a description is,
    then every run of punctuation, underscores and whitespace one space, ends trimmed.
    Patterns match in this form, and a label or slug meets its row's patterns in it."""
    return _NOT_WORD.sub(" ", normalize_description(text)).strip()


def _normalized(patterns: tuple[str, ...]) -> tuple[str, ...]:
    """Each pattern normalized as a description is, repeats dropped, order kept."""
    return tuple(dict.fromkeys(normalize_description(each) for each in patterns))


def _matchable(patterns: tuple[str, ...]) -> tuple[str, ...]:
    """A row's patterns, each with something left to match once folded."""
    if not all(compared(each) for each in patterns):
        raise ValueError("a pattern holds no letter or digit")
    return _normalized(patterns)


Patterns = Annotated[
    tuple[Text, ...], BeforeValidator(split_list), AfterValidator(_matchable)
]
Slugs = Annotated[tuple[Slug, ...], BeforeValidator(split_list)]


class PayeeRow(BaseModel):
    """One ``payees.csv`` row; ``patterns`` are held normalized."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    slug: Slug
    label: Text
    kind: PayeeKind
    patterns: Patterns = ()
    party: Slug | None = None
    paid_from: Slugs = ()

    @model_validator(mode="after")
    def _no_printed_name_as_label(self) -> Self:
        printed = {compared(pattern) for pattern in self.patterns}
        for field_name, value in (("slug", self.slug), ("label", self.label)):
            if compared(value) in printed:
                raise ValueError(
                    f"{field_name}: equals one of the row's own patterns; a"
                    f" {field_name} is a word you choose, never the printed name"
                )
        return self


@dataclass(frozen=True)
class Payee:
    """One payee. ``account`` is the slug of the account it stands for (kind
    ``account`` only)."""

    slug: str
    label: str
    kind: PayeeKind
    patterns: tuple[str, ...]
    account: str | None = None
    party: str | None = None
    paid_from: tuple[str, ...] = ()

    def names(self, text: str) -> bool:
        """Whether one of the patterns is a whole-token run of ``text``, both folded."""
        return names_any(self.patterns, text)


def names_any(patterns: Iterable[str], text: str) -> bool:
    """Whether one of ``patterns`` is a whole-token run of ``text``, both folded
    (``compared``). A folded text is its tokens joined by single spaces, so a run is a
    substring between spaces. A pattern that folds to nothing names nothing."""
    tokens = f" {compared(text)} "
    return any(f" {folded} " in tokens for folded in map(compared, patterns) if folded)


@dataclass(frozen=True)
class Payees:
    """Every payee, account-backed first, then ``payees.csv`` in file order, then the
    bill issuers; and every ``payees.csv`` error, sorted."""

    payees: tuple[Payee, ...]
    errors: tuple[RowError, ...]

    @property
    def slugs(self) -> frozenset[str]:
        return frozenset(payee.slug for payee in self.payees)

    def patterns_naming(self, printed: str) -> tuple[str, ...]:
        """The patterns of every payee one of whose patterns names ``printed`` (a
        receipt's or invoice's printed payee), in payee order, without repeats."""
        found: list[str] = []
        for payee in self.payees:
            if payee.names(printed):
                found.extend(p for p in payee.patterns if p not in found)
        return tuple(found)


def bill_issuers() -> tuple[str, ...]:
    """The issuer keys whose archive directory holds bills, sorted."""
    return tuple(
        sorted(key for key, rule in issuers.ISSUER_DIRS.items() if rule.kind == "bill")
    )


def build_payees(result: LoadResult[PayeeRow], registry: Registry) -> Payees:
    errors = list(result.errors)

    def reject(line: int, reason: str) -> None:
        errors.append(RowError(PAYEES_FILE, line, reason))

    accounts = {row.slug: row for row in registry.accounts}
    backed = [
        Payee(
            slug=row.slug,
            label=row.label,
            kind="account",
            patterns=_normalized(row.payee_patterns),
            account=row.slug,
        )
        for row in registry.accounts
        if row.payee_patterns
    ]
    taken = {payee.slug for payee in backed}

    rows = result.numbered()
    bad = duplicate_lines([(line, row.slug) for line, row in rows])
    for line in sorted(bad):
        reject(line, "slug: another row has the same slug")
    check = PathCheck(
        ident.identifier for ident in registry.identifiers if ident.kind != "internal"
    )
    for line, row in rows:
        if row.slug in taken:
            reject(
                line,
                f"slug: the account-backed payee of {ACCOUNTS_FILE} payee_patterns"
                " has this slug",
            )
            bad.add(line)
        if row.kind == "account" and row.slug not in accounts:
            reject(line, "slug: a payee of kind account takes a loaded account's slug")
            bad.add(line)
        if any(slug not in accounts for slug in row.paid_from):
            reject(line, "paid_from: no loaded account has this slug")
            bad.add(line)
        for field_name, value in (("slug", row.slug), ("label", row.label)):
            if not check.allows(value):
                reject(line, f"{field_name}: {CARRIES_IDENTIFIER}")
                bad.add(line)
    listed = [
        Payee(
            slug=row.slug,
            label=row.label,
            kind=row.kind,
            patterns=row.patterns,
            account=row.slug if row.kind == "account" else None,
            party=row.party,
            paid_from=row.paid_from,
        )
        for line, row in rows
        if line not in bad
    ]
    taken |= {payee.slug for payee in listed}
    vendors = [
        Payee(slug=key, label=key, kind="vendor", patterns=())
        for key in bill_issuers()
        if key not in taken
    ]
    return Payees((*backed, *listed, *vendors), tuple(sorted(set(errors))))
