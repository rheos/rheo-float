"""The test-only crypto fixture parser: synthetic crypto lines for the derive tests.

No crypto parser ships yet (the CoinStats and exchange parsers come with their
features), so the crypto stages are tested on lines this parser emits from a small
synthetic layout. It is registered only inside a test, by the ``crypto_ledger``
fixture (``tests/derive/conftest.py``), under the test-only issuer directory
``CRYPTO_ISSUER``; it is never in ``parsers.registry.PARSER_MODULES`` and nothing under
``src/`` names it (``test_crypto_keys`` checks both).

The layout, one item per line:

- ``SYNTHETIC CRYPTO LINES`` first;
- ``ACCOUNT <identifier>``: the account the lines after it are on, matched as a
  ``number`` identifier at ``CRYPTO_INSTITUTION``;
- ``MOVE key=value ...``: one ``crypto_movement`` line. ``at`` (ISO time with offset)
  and ``asset`` and ``qty`` (signed, as printed: ``1E-7`` stays ``1E-7``) are
  required; ``txid``, ``to``, ``from``, ``type`` (source type), ``cad`` (stated CAD
  worth), ``trade`` (trade ref), ``fake=1``, ``hint=1`` (the source marks it a
  transfer), ``rev=1`` (the source marks it a reversal) and ``feestated=1`` are
  optional;
- ``FEE key=value ...``: one ``crypto_fee`` line, ``qty`` the positive fee;
- ``WITHDRAW key=value ...``: a withdrawal through ``parsers.crypto.withdrawal_lines``,
  ``qty`` the positive printed figure, with optional ``fee``, ``gross=1`` (the figure
  includes the fee) and ``feeasset``.

Two formats are registered (``txt`` and ``csv``), so two documents can rank apart: a
CSV ranks above a text file of equal control (``derive.precedence``). Every value a
test writes is invented: addresses and TXIDs are made-up strings, quantities and
dates are arbitrary.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Final

from rheo_finance.parsers.base import (
    CRYPTO_FEE,
    CRYPTO_MOVEMENT,
    AccountLookup,
    NotRecognised,
    ParsedLine,
    ParseResult,
    RegisteredAccount,
    Rejected,
)
from rheo_finance.parsers.crypto import Withdrawal, withdrawal_lines
from rheo_finance.parsers.registry import Parser

CRYPTO_ISSUER: Final = "synthetic-crypto"
CRYPTO_INSTITUTION: Final = "Sample Crypto"
CRYPTO_HEADER: Final = "SYNTHETIC CRYPTO LINES"
CRYPTO_PARSER_PREFIX: Final = "synthetic_crypto_"
# Identifiers of two fixture accounts (``number`` kind).
WALLET_ID: Final = "999000000081"
EXCHANGE_ID: Final = "999000000082"


def _fields(rest: str) -> dict[str, str]:
    found = {}
    for part in rest.split():
        key, _, value = part.partition("=")
        found[key] = value
    return found


def _when(text: str) -> dt.datetime:
    value = dt.datetime.fromisoformat(text)
    if value.tzinfo is None:
        raise NotRecognised
    return value


def _line(kind: str, account: RegisteredAccount, f: dict[str, str]) -> ParsedLine:
    qty = Decimal(f["qty"])
    return ParsedLine(
        line_kind=CRYPTO_MOVEMENT if kind == "MOVE" else CRYPTO_FEE,
        account=account,
        occurred_at=_when(f["at"]),
        amount=qty if kind == "MOVE" else -qty,
        asset_identifier=f["asset"],
        txid=f.get("txid"),
        to_address=f.get("to"),
        from_address=f.get("from"),
        source_type=f.get("type"),
        stated_cad=Decimal(f["cad"]) if "cad" in f else None,
        trade_ref=f.get("trade"),
        is_fake=f.get("fake") == "1",
        transfer_hint=f.get("hint") == "1",
        reversal_hint=f.get("rev") == "1",
        fee_stated=kind == "FEE" or f.get("feestated") == "1",
        fee_type="withdrawal_fee" if kind == "FEE" else None,
    )


def parse_crypto(text: str, accounts: AccountLookup) -> ParseResult:
    rows = [row.strip() for row in text.splitlines() if row.strip()]
    if not rows or rows[0] != CRYPTO_HEADER:
        raise NotRecognised
    account: RegisteredAccount | None = None
    lines: list[ParsedLine] = []
    for row in rows[1:]:
        head, _, rest = row.partition(" ")
        if head == "ACCOUNT":
            day = dt.date(2000, 1, 1)
            matched = accounts.by_identifier(CRYPTO_INSTITUTION, rest, day, day)
            if len(matched) != 1:
                raise Rejected("unknown_account")
            account = matched[0]
            continue
        if account is None:
            raise NotRecognised
        f = _fields(rest)
        if head in ("MOVE", "FEE"):
            lines.append(_line(head, account, f))
        elif head == "WITHDRAW":
            lines.extend(
                withdrawal_lines(
                    Withdrawal(
                        account=account,
                        occurred_at=_when(f["at"]),
                        asset_identifier=f["asset"],
                        quantity=Decimal(f["qty"]),
                        fee=Decimal(f["fee"]) if "fee" in f else None,
                        gross=f.get("gross") == "1",
                        fee_asset=f.get("feeasset"),
                        txid=f.get("txid"),
                        to_address=f.get("to"),
                        from_address=f.get("from"),
                        source_type=f.get("type"),
                    )
                )
            )
        else:
            raise NotRecognised
    return ParseResult(periods=(), lines=tuple(lines), control_kind="none")


CRYPTO_PARSERS: Final = tuple(
    Parser(f"{CRYPTO_PARSER_PREFIX}{fmt}", CRYPTO_ISSUER, fmt, "1", parse_crypto)
    for fmt in ("txt", "csv")
)


def crypto_document(*items: str) -> str:
    """A fixture document of ``items`` (``ACCOUNT ...``, ``MOVE ...`` and the like)."""
    return "\n".join([CRYPTO_HEADER, *items]) + "\n"
