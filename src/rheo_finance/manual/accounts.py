"""The account registry from ``accounts.csv`` and ``account_identifiers.csv`` (FR-1).

``AccountRow`` and ``AccountIdentifierRow`` validate one row each. ``build_registry``
then applies the rules that span rows and files, and records every failure on every row
involved:

- a slug used by two account rows, or the built-in account's slug;
- an identifier row naming no loaded account (the ``account`` column holds a slug);
- one identifier (same kind, same text) on two rows;
- two ``number`` identifiers of different accounts at one institution (compared
  case-insensitively) that end in the same four digits: the reused Vancity parser
  matches an account on its number's last four only (Key decision 1);
- an account left with no identifier row (AC-1), checked after the rules above.

The built-in account "Paid outside recorded accounts" (kind ``wallet``, identifier
``CASH`` of kind ``internal``) is added here, never read from a file, so it exists
after every load. Kind ``internal`` is reserved for it.

Adding an account is data entry alone: a row in each file, no code change.
"""

from __future__ import annotations

import datetime as dt
import re
from collections import defaultdict
from dataclasses import dataclass
from typing import Annotated, Final, Literal, Self

from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    StringConstraints,
    model_validator,
)

from rheo_finance.manual.loader import LoadResult, RowError, split_list

ACCOUNTS_FILE: Final = "accounts.csv"
IDENTIFIERS_FILE: Final = "account_identifiers.csv"

AccountKind = Literal[
    "chequing", "savings", "credit_card", "wallet", "exchange", "payment_processor",
    "bill_vendor", "crypto_wallet",
]  # fmt: skip
IdentifierKind = Literal[
    "number", "masked_card", "file_token", "portfolio_name", "internal"
]
# How often a statement is expected; ``none`` for an account with no statements.
Cadence = Literal["monthly", "quarterly", "yearly", "none"]
SourcedBy = Literal["documents", "mail", "api"]
MissingReason = Literal["mail_purged"]

Slug = Annotated[str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9_-]*$")]
Text = Annotated[str, StringConstraints(min_length=1)]


def _upper_all(values: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(value.upper() for value in values)


Currency = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9]{2,12}$")]
Currencies = Annotated[
    tuple[Currency, ...],
    BeforeValidator(split_list),
    AfterValidator(_upper_all),
    Field(min_length=1),
]
TextList = Annotated[tuple[Text, ...], BeforeValidator(split_list)]


class AccountRow(BaseModel):
    """One ``accounts.csv`` row. List cells (currencies, mail senders, payee patterns)
    hold ``;``-separated values."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    slug: Slug
    label: Text
    institution: Text
    kind: AccountKind
    currencies: Currencies
    cadence: Cadence
    owner: Text | None = None
    opened: dt.date | None = None
    closed: dt.date | None = None
    sourced_by: SourcedBy = "documents"
    mail_senders: TextList = ()
    payee_patterns: TextList = ()
    placeholder: bool = False
    missing_before: dt.date | None = None
    missing_reason: MissingReason | None = None

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if self.opened and self.closed and self.closed < self.opened:
            raise ValueError("closed is before opened")
        if self.placeholder and self.kind != "crypto_wallet":
            raise ValueError("placeholder is for a crypto_wallet account only")
        if (self.missing_before is None) != (self.missing_reason is None):
            raise ValueError("missing_before and missing_reason go together")
        return self


class AccountIdentifierRow(BaseModel):
    """One ``account_identifiers.csv`` row; ``account`` is the account's slug."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    account: Slug
    identifier: Text
    kind: IdentifierKind
    valid_from: dt.date | None = None
    valid_to: dt.date | None = None

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if self.valid_from and self.valid_to and self.valid_to < self.valid_from:
            raise ValueError("valid_to is before valid_from")
        return self


INTERNAL_ACCOUNT: Final = AccountRow(
    slug="paid-outside",
    label="Paid outside recorded accounts",
    institution="internal",
    kind="wallet",
    currencies=("CAD",),
    cadence="none",
)
INTERNAL_IDENTIFIER: Final = AccountIdentifierRow(
    account=INTERNAL_ACCOUNT.slug, identifier="CASH", kind="internal"
)

_LAST_FOUR = re.compile(r"(\d)\D*(\d)\D*(\d)\D*(\d)\D*$")


def last_four(identifier: str) -> str | None:
    """The last four digits of ``identifier`` (separators ignored), or None."""
    match = _LAST_FOUR.search(identifier)
    return "".join(match.groups()) if match else None


@dataclass(frozen=True)
class Registry:
    """The accounts and identifiers that passed every rule, the built-in account
    included, and every error from both files, sorted and without repeats."""

    accounts: tuple[AccountRow, ...]
    identifiers: tuple[AccountIdentifierRow, ...]
    errors: tuple[RowError, ...]


def _duplicates[K](keyed: list[tuple[int, K]]) -> set[int]:
    """The line numbers whose key appears on more than one line."""
    lines: dict[K, list[int]] = defaultdict(list)
    for line, key in keyed:
        lines[key].append(line)
    return {line for group in lines.values() if len(group) > 1 for line in group}


def build_registry(
    accounts: LoadResult[AccountRow], identifiers: LoadResult[AccountIdentifierRow]
) -> Registry:
    errors = [*accounts.errors, *identifiers.errors]

    def reject(file: str, line: int, reason: str) -> None:
        errors.append(RowError(file, line, reason))

    # Accounts: unique slugs, and none taking the built-in account's.
    account_rows = accounts.numbered()
    bad_accounts = _duplicates([(line, row.slug) for line, row in account_rows])
    for line in sorted(bad_accounts):
        reject(ACCOUNTS_FILE, line, "slug: another row has the same slug")
    for line, row in account_rows:
        if row.slug == INTERNAL_ACCOUNT.slug:
            reject(ACCOUNTS_FILE, line, "slug: reserved for the built-in account")
            bad_accounts.add(line)
    by_slug = {row.slug: row for line, row in account_rows if line not in bad_accounts}

    # Identifiers: a loaded account, a kind that is not reserved, unique text.
    identifier_rows = identifiers.numbered()
    bad_identifiers: set[int] = set()
    for line, ident in identifier_rows:
        if ident.account not in by_slug:
            reject(IDENTIFIERS_FILE, line, "account: no loaded account has this slug")
            bad_identifiers.add(line)
        if ident.kind == "internal":
            reject(IDENTIFIERS_FILE, line, "kind: internal is for the built-in account")
            bad_identifiers.add(line)
    repeated = _duplicates(
        [(line, (ident.kind, ident.identifier)) for line, ident in identifier_rows]
    )
    for line in sorted(repeated):
        reject(IDENTIFIERS_FILE, line, "identifier: another row has the same one")
    bad_identifiers |= repeated

    # Two accounts at one institution whose numbers share their last four.
    by_four: dict[tuple[str, str], list[tuple[int, str]]] = defaultdict(list)
    for line, ident in identifier_rows:
        account = by_slug.get(ident.account)
        four = last_four(ident.identifier)
        if account is None or ident.kind != "number" or four is None:
            continue
        by_four[(account.institution.casefold(), four)].append((line, account.slug))
    for group in by_four.values():
        if len({slug for _, slug in group}) > 1:
            for line, _ in group:
                reject(
                    IDENTIFIERS_FILE,
                    line,
                    "identifier: another account at this institution has a number"
                    " with the same last four digits",
                )
                bad_identifiers.add(line)

    kept_identifiers = [
        ident for line, ident in identifier_rows if line not in bad_identifiers
    ]
    identified = {ident.account for ident in kept_identifiers}
    kept_accounts = []
    for line, row in account_rows:
        if line in bad_accounts:
            continue
        if row.slug not in identified:
            reject(ACCOUNTS_FILE, line, "the account has no valid identifier row")
            continue
        kept_accounts.append(row)

    return Registry(
        accounts=(INTERNAL_ACCOUNT, *kept_accounts),
        identifiers=(INTERNAL_IDENTIFIER, *kept_identifiers),
        errors=tuple(sorted(set(errors))),
    )
