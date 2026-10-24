"""The seam ``reference.assets.asset_code`` (SPEC "Data Models" > Asset codes): source
identifiers to canonical codes, and ``cs:<identifier>`` for any other.

The identifiers are the generic forms the sources print (a tracker's coin slugs, an
exchange's tickers); the unlisted ones are invented.
"""

from __future__ import annotations

import hashlib
import importlib.resources

import pytest
from pydantic import ValidationError

from rheo_finance.reference.assets import (
    ASSETS_FILE,
    AssetFile,
    asset_code,
    asset_map,
    assets_digest,
)


@pytest.mark.parametrize(
    ("identifier", "code"),
    [("bitcoin", "BTC"), ("BTC", "BTC"), ("ethereum", "ETH"), ("ETH", "ETH"),
     ("tether", "USDT"), ("USDT", "USDT"), ("usd-coin", "USDC"), ("USDC", "USDC"),
     ("litecoin", "LTC"), ("LTC", "LTC"), ("solana", "SOL"), ("SOL", "SOL")],
)  # fmt: skip
def test_each_listed_identifier_gives_its_code(identifier: str, code: str) -> None:
    assert asset_code(identifier) == code


@pytest.mark.parametrize(
    "identifier", ["tether-claim-999", "Bitcoin", "usdt", "0x999aBcD", "cs:USDT"]
)
def test_an_unlisted_identifier_gets_its_own_code(identifier: str) -> None:
    # Matched exactly and with case: a token printing a real symbol in another form
    # never takes the real asset's code.
    assert asset_code(identifier) == f"cs:{identifier}"
    assert asset_code(identifier) not in set(asset_map().values())


def test_the_file_refuses_an_identifier_under_two_codes() -> None:
    with pytest.raises(ValidationError, match="two codes"):
        AssetFile.model_validate({"codes": {"BTC": ["bitcoin"], "ETH": ["bitcoin"]}})


def test_the_file_refuses_an_identifier_shaped_like_an_unmapped_code() -> None:
    with pytest.raises(ValidationError, match="starts with"):
        AssetFile.model_validate({"codes": {"USDT": ["cs:tether"]}})


def test_the_digest_is_the_files_sha256() -> None:
    data = importlib.resources.files("rheo_finance.reference").joinpath(ASSETS_FILE)
    assert assets_digest() == hashlib.sha256(data.read_bytes()).hexdigest()
