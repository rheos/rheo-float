"""Fixtures of the derive tests alone: ``card_parsers``, which maps the test-only card
issuer directory and registers its parser for one test; ``own``, the accounts of the
transfer-edge tests; and ``crypto_ledger``, ``ledger`` with the test-only crypto fixture
parser (``crypto_support``). ``ledger`` and ``fx_ledger`` are in ``tests/conftest.py``:
other directories use them too (``tests/guards/test_test_layout.py``).
"""

from __future__ import annotations

from types import MappingProxyType

import pytest

from rheo_finance.documents import issuers
from rheo_finance.parsers import registry

from .crypto_support import (
    CRYPTO_INSTITUTION,
    CRYPTO_ISSUER,
    CRYPTO_PARSERS,
    EXCHANGE_ID,
    WALLET_ID,
)
from .support import (
    CARD_ISSUER,
    CARD_PARSERS,
    CSV_IDENTIFIER,
    FX2_IDENTIFIER,
    FX_IDENTIFIER,
    FX_INSTITUTION,
    SECOND,
    THIRD,
    Ledger,
)


@pytest.fixture
def card_parsers(monkeypatch: pytest.MonkeyPatch) -> None:
    """Map ``CARD_ISSUER`` as a statement issuer (a CSV there is an export) and
    register the card parsers, for this test only."""
    monkeypatch.setattr(
        issuers,
        "ISSUER_DIRS",
        MappingProxyType(
            {
                **issuers.ISSUER_DIRS,
                CARD_ISSUER: issuers.IssuerDir("statement", {"csv": "export"}),
            }
        ),
    )
    registry.load_parsers()
    for parser in CARD_PARSERS:
        monkeypatch.setitem(registry.PARSERS, parser.parser_id, parser)


@pytest.fixture
def own(fx_ledger: Ledger) -> Ledger:
    """Three chequing accounts and a Visa card at Vancity, and two accounts at the
    synthetic FX institution (``FX_LINES`` documents under ``FX_ISSUER``)."""
    fx_ledger.accounts(
        ("chequing", "Vancity", "chequing", CSV_IDENTIFIER),
        ("second", "Vancity", "chequing", SECOND),
        ("third", "Vancity", "chequing", THIRD),
        ("visa", "Vancity", "credit_card", "999000009991"),
        ("fx", FX_INSTITUTION, "chequing", FX_IDENTIFIER),
        ("fx2", FX_INSTITUTION, "chequing", FX2_IDENTIFIER),
    )
    return fx_ledger


@pytest.fixture
def crypto_ledger(ledger: Ledger, monkeypatch: pytest.MonkeyPatch) -> Ledger:
    """``ledger`` with two crypto accounts at ``CRYPTO_INSTITUTION`` (slugs ``wallet``,
    kind ``crypto_wallet``, and ``exchange``, kind ``exchange``) and the
    ``CRYPTO_ISSUER`` directory mapped to the crypto fixture parsers, for this test
    only. Tests that need other accounts write ``accounts.csv`` again."""
    monkeypatch.setattr(
        issuers,
        "ISSUER_DIRS",
        MappingProxyType(
            {**issuers.ISSUER_DIRS, CRYPTO_ISSUER: issuers.IssuerDir("export")}
        ),
    )
    registry.load_parsers()
    for parser in CRYPTO_PARSERS:
        monkeypatch.setitem(registry.PARSERS, parser.parser_id, parser)
    ledger.accounts(
        ("wallet", CRYPTO_INSTITUTION, "crypto_wallet", WALLET_ID),
        ("exchange", CRYPTO_INSTITUTION, "exchange", EXCHANGE_ID),
    )
    return ledger
