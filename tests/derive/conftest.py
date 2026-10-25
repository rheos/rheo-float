"""The ``ledger`` fixture for the derive tests; ``card_parsers``, which maps the
test-only card issuer directory and registers its parser for one test; and
``fx_ledger``, the same for the conversion fixtures; and ``crypto_ledger``, the same
for the test-only crypto fixture parser (``crypto_support``)."""

from __future__ import annotations

from pathlib import Path
from types import MappingProxyType

import pytest
from sqlalchemy import Connection

from rheo_finance.contracts import Role
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
    FX_IDENTIFIER,
    FX_INSTITUTION,
    FX_ISSUER,
    FX_PARSER,
    Ledger,
    MakeContext,
)


@pytest.fixture
def ledger(
    owner_conn: Connection,
    rolled_back_context: MakeContext,
    tmp_path: Path,
) -> Ledger:
    return Ledger(owner_conn, rolled_back_context(Role.OWNER), tmp_path)


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
def fx_ledger(ledger: Ledger, monkeypatch: pytest.MonkeyPatch) -> Ledger:
    """``ledger`` with one chequing account at ``FX_INSTITUTION`` (slug ``fx``) and
    the ``FX_ISSUER`` directory mapped to ``parse_fx``, for this test only."""
    monkeypatch.setattr(
        issuers,
        "ISSUER_DIRS",
        MappingProxyType(
            {**issuers.ISSUER_DIRS, FX_ISSUER: issuers.IssuerDir("statement")}
        ),
    )
    registry.load_parsers()
    monkeypatch.setitem(registry.PARSERS, FX_PARSER.parser_id, FX_PARSER)
    ledger.accounts(("fx", FX_INSTITUTION, "chequing", FX_IDENTIFIER))
    return ledger


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
