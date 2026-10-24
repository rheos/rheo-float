"""The owner's own wallet addresses, from ``own_addresses.csv`` (FR-10, FR-15(b); SPEC
"Data Models" ``own_address``).

``OwnAddressRow`` validates one row: the account the address belongs to (an account
slug) and the address, held in its compared form (``normalize_address``): a hex
address (``0x`` then hex digits) lower-cased, since its case carries no meaning, and
any other address as printed, since a base58 address is case-sensitive.
``build_own_addresses`` rejects a row whose account is not in the loaded registry, and
every row of an address given for two accounts, since a destination is on one
account; the same address repeated for one account loads once.

Stage 2 writes the rows into ``own_address`` (restricted). Stage 7 reads them: a
crypto line whose counterpart address is an own address is a transfer between own
accounts. The registry helper drafts rows into a file the loader never reads; it
never writes this file.
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Collection
from dataclasses import dataclass
from typing import Annotated, Final

from pydantic import AfterValidator, BaseModel, ConfigDict

from rheo_finance.manual.accounts import Slug, Text
from rheo_finance.manual.loader import LoadResult, RowError

OWN_ADDRESSES_FILE: Final = "own_addresses.csv"
_HEX = re.compile(r"0[xX][0-9a-fA-F]+")


def normalize_address(address: str) -> str:
    """An address in its compared form: stripped, and lower-cased when it is hex."""
    address = address.strip()
    return address.lower() if _HEX.fullmatch(address) else address


def _address(value: str) -> str:
    if any(char.isspace() for char in value):
        raise ValueError("the address holds whitespace")
    return normalize_address(value)


class OwnAddressRow(BaseModel):
    """One ``own_addresses.csv`` row; ``address`` is held normalized."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    account: Slug
    address: Annotated[Text, AfterValidator(_address)]


@dataclass(frozen=True)
class OwnAddresses:
    """The rows that passed every rule, one per address, and every error, sorted."""

    rows: tuple[OwnAddressRow, ...]
    errors: tuple[RowError, ...]


def build_own_addresses(
    result: LoadResult[OwnAddressRow], account_slugs: Collection[str]
) -> OwnAddresses:
    numbered = result.numbered()
    errors = list(result.errors)
    unknown = {line for line, row in numbered if row.account not in account_slugs}
    errors.extend(
        RowError(OWN_ADDRESSES_FILE, line, "account: no loaded account has this slug")
        for line in sorted(unknown)
    )
    accounts: dict[str, set[str]] = defaultdict(set)
    for line, row in numbered:
        if line not in unknown:
            accounts[row.address].add(row.account)
    split = {
        line
        for line, row in numbered
        if line not in unknown and len(accounts[row.address]) > 1
    }
    errors.extend(
        RowError(
            OWN_ADDRESSES_FILE, line, "address: another row gives it another account"
        )
        for line in sorted(split)
    )
    kept: dict[str, OwnAddressRow] = {}
    for line, row in numbered:
        if line not in unknown | split:
            kept.setdefault(row.address, row)
    return OwnAddresses(tuple(kept.values()), tuple(sorted(set(errors))))
