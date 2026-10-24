"""Asset codes: a crypto line's source identifier to its canonical code (SPEC "Data
Models" > Asset codes, Derivation stage 3).

``asset_code(identifier)`` reads ``assets.json``: each canonical code (``BTC``,
``USDT`` and the like) lists the source identifiers that name it, matched exactly and
with case, so the tracker's coin slug and an exchange's ticker of one asset give one
code. An identifier the file does not list gets ``cs:<identifier>``: a token whose
own identifier is not a listed one never shares a code with a real asset, whatever
symbol it prints, so it never pairs with, prices as or nets against it.

The code is what a crypto txn carries as its currency and what pairing and netting
compare; the identifier stays on the line as printed.
"""

from __future__ import annotations

import functools
import hashlib
import importlib.resources
from collections.abc import Mapping
from types import MappingProxyType
from typing import Annotated, Final, Self

from pydantic import BaseModel, ConfigDict, StringConstraints, model_validator

ASSETS_FILE: Final = "assets.json"
# The code of an identifier ``assets.json`` does not list: this prefix, then it.
UNMAPPED_PREFIX: Final = "cs:"

Code = Annotated[str, StringConstraints(pattern=r"^[A-Z0-9]{2,10}$")]
Identifier = Annotated[str, StringConstraints(min_length=1, pattern=r"^\S+$")]


class AssetFile(BaseModel):
    """``assets.json``: ``about`` notes and each code's identifiers. An identifier
    listed under two codes, or one shaped like an unmapped code, is refused."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    about: tuple[str, ...] = ()
    codes: Mapping[Code, tuple[Identifier, ...]]

    @model_validator(mode="after")
    def _one_code_per_identifier(self) -> Self:
        seen: set[str] = set()
        for identifiers in self.codes.values():
            for identifier in identifiers:
                if identifier in seen:
                    raise ValueError("an identifier is listed under two codes")
                if identifier.startswith(UNMAPPED_PREFIX):
                    raise ValueError(f"an identifier starts with {UNMAPPED_PREFIX!r}")
                seen.add(identifier)
        return self


def _assets_bytes() -> bytes:
    return importlib.resources.files(__package__).joinpath(ASSETS_FILE).read_bytes()


@functools.cache
def asset_map() -> Mapping[str, str]:
    """Every listed identifier to its code, read and validated once."""
    parsed = AssetFile.model_validate_json(_assets_bytes().decode("utf-8"))
    return MappingProxyType(
        {
            identifier: code
            for code, identifiers in parsed.codes.items()
            for identifier in identifiers
        }
    )


@functools.cache
def assets_digest() -> str:
    """sha256 of ``assets.json``'s bytes. Stage 3 keys and stores crypto lines by the
    codes it gives, so the input fingerprint hashes it."""
    return hashlib.sha256(_assets_bytes()).hexdigest()


def asset_code(identifier: str) -> str:
    """The canonical code of a source identifier, ``cs:<identifier>`` when the file
    does not list it."""
    return asset_map().get(identifier, f"{UNMAPPED_PREFIX}{identifier}")
