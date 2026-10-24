"""Derivation stage 3 for crypto lines: their natural keys, one txn per key, and the
quantity discrepancy (FR-4, FR-13, Key decision 5, edge case 48).

**Which lines.** ``crypto_movement`` and ``crypto_fee`` lines of ``parsed`` documents
with an account, an ``occurred_at``, a quantity (``amount``, signed) and an asset
identifier; a line missing one of them is left out and counted
(``Lines.incomplete_lines``).

**Natural keys.** The asset code is ``reference.assets.asset_code`` of the line's
identifier. Keys are UTC throughout, so changing ``finance.locale.timezone`` re-keys
nothing and orphans no decision:

- a line with a TXID: (account, asset code, TXID, direction, occurrence), the
  direction ``in`` or ``out`` by the quantity's sign, the occurrence counted among the
  lines of the same four in one document. No quantity, so an email that prints a gross
  figure and an export that prints a net one are one txn with two sources;
- its fee line (kind ``crypto_fee``): (account, asset code, TXID, ``fee``), with the
  same occurrence count, which is 1 unless a document prints two fee lines of one
  asset for one TXID;
- a line with no TXID: (account, ``occurred_at`` to the second in UTC, asset
  identifier, signed quantity, source type, line kind, occurrence). A quantity enters
  the key as a number (``storage.ids`` canonicalizes it), so ``1E-7`` and
  ``0.0000001`` are one key. The line kind keeps a fee and a movement of the same
  figure at the same second apart in every document, whatever order it prints them
  in.

Each line's ``line_key_id`` is ``derived_id`` of its key on the UTC calendar date of
its own ``occurred_at``, and a txn's id is its primary line's. ``txn.posting_date`` is
the local date of ``occurred_at`` in ``finance.locale.timezone``, so it follows the
setting while the id does not.

**One txn per key.** Lines of one key from several documents are one txn: the line of
the document that ranks first on the account (``precedence.standings``: control,
format, then sha256) is its primary source, every other an additional one. When an
additional line prints another quantity, the primary's stands and the other line is
also a ``quantity_mismatch`` source discrepancy, which ``status`` counts. A crypto
account has no tied-out statement, so no window refuses a line and no txn is
provisional.

**What the txn carries.** The signed quantity as ``amount``, the asset code as
``currency``, the identifier as printed, the TXID and addresses, ``fee_type`` and
``trade_ref`` as the parser gave them, and ``movement_kind`` ``crypto_fee`` for a fee
line, else ``crypto_unclassified`` until stage 7 classifies it.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections import Counter, defaultdict
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Final
from zoneinfo import ZoneInfo

from sqlalchemy import Row, bindparam, insert, select, update

from rheo_finance.context import UnitOfWork
from rheo_finance.derive.precedence import Standing, standings
from rheo_finance.reference.assets import asset_code
from rheo_finance.storage.ids import derived_id
from rheo_finance.storage.schema import (
    account,
    document,
    document_line,
    source_discrepancy,
    txn,
    txn_source,
)
from rheo_finance.text import normalize_description

MOVEMENT: Final = "crypto_movement"
FEE: Final = "crypto_fee"
LINE_KINDS: Final = (MOVEMENT, FEE)
QUANTITY_MISMATCH: Final = "quantity_mismatch"
# The first part of each key shape, so the two shapes never share a key.
TXID_KEY: Final = "crypto_txid"
TIME_KEY: Final = "crypto_time"
FEE_DIRECTION: Final = "fee"
UNCLASSIFIED: Final = "crypto_unclassified"


@dataclass(frozen=True)
class CryptoLines:
    """What the crypto part of stage 3 wrote, as counts."""

    txns: int = 0
    additional_sources: int = 0
    discrepancies: int = 0
    incomplete_lines: int = 0


@dataclass(frozen=True)
class _Keyed:
    row: Row[Any]
    code: str
    # occurred_at in UTC, to the second.
    at: dt.datetime
    key: uuid.UUID


def key_base(
    *,
    slug: str,
    code: str,
    identifier: str,
    txid: str | None,
    fee: bool,
    quantity: Decimal,
    at: dt.datetime,
    source_type: str | None,
) -> tuple[object, ...]:
    """A crypto line's natural key without its occurrence (module docstring)."""
    if txid is not None:
        direction = FEE_DIRECTION if fee else ("in" if quantity > 0 else "out")
        return (TXID_KEY, slug, code, txid, direction)
    return (TIME_KEY, slug, at, identifier, quantity, source_type,
            FEE if fee else MOVEMENT)  # fmt: skip


def _utc_second(value: dt.datetime) -> dt.datetime:
    return value.astimezone(dt.UTC).replace(microsecond=0)


def _rank(found: dict[uuid.UUID, list[Standing]], account_id: uuid.UUID,
          document_id: uuid.UUID) -> int:  # fmt: skip
    for place, each in enumerate(found.get(account_id, ())):
        if each.document_id == document_id:
            return place
    # Every parsed document with a line on the account has a standing there.
    raise LookupError("a crypto line's document has no standing on its account")


def build_crypto_lines(uow: UnitOfWork, zone: ZoneInfo) -> CryptoLines:
    """Write the crypto txns, their sources and the quantity discrepancies, and store
    each keyed line's key and normalized wording on ``document_line``."""
    rows = uow.execute(
        select(
            document_line.c.id,
            document_line.c.document_id,
            document_line.c.line_no,
            document_line.c.line_kind,
            document_line.c.account_id,
            account.c.slug,
            document_line.c.occurred_at,
            document_line.c.amount,
            document_line.c.asset_identifier,
            document_line.c.description_raw,
            document_line.c.counterparty,
            document_line.c.txid,
            document_line.c.from_address,
            document_line.c.to_address,
            document_line.c.source_type,
            document_line.c.fee_type,
            document_line.c.trade_ref,
        )
        .join(document, document.c.id == document_line.c.document_id)
        .outerjoin(account, account.c.id == document_line.c.account_id)
        .where(document.c.status == "parsed", document_line.c.line_kind.in_(LINE_KINDS))
        .order_by(document.c.sha256, document_line.c.line_no)
    ).all()
    occurrences: Counter[tuple[uuid.UUID, tuple[object, ...]]] = Counter()
    # By natural key, not by id: two sources of one TXID may print times on either
    # side of a UTC midnight, and the id's time field is each line's own date.
    by_key: dict[tuple[object, ...], list[_Keyed]] = defaultdict(list)
    incomplete = 0
    for row in rows:
        if (
            row.slug is None
            or row.occurred_at is None
            or row.amount is None
            or row.asset_identifier is None
        ):
            incomplete += 1
            continue
        at = _utc_second(row.occurred_at)
        code = asset_code(row.asset_identifier)
        base = key_base(
            slug=row.slug,
            code=code,
            identifier=row.asset_identifier,
            txid=row.txid,
            fee=row.line_kind == FEE,
            quantity=row.amount,
            at=at,
            source_type=row.source_type,
        )
        occurrences[row.document_id, base] += 1
        natural = (*base, occurrences[row.document_id, base])
        by_key[natural].append(_Keyed(row, code, at, derived_id(at.date(), natural)))

    found = standings(uow)
    txns: list[dict[str, object]] = []
    sources: list[dict[str, object]] = []
    discrepancies: list[dict[str, object]] = []
    for lines in by_key.values():
        ranked = sorted(
            lines,
            key=lambda each: (
                _rank(found, each.row.account_id, each.row.document_id),
                each.row.line_no,
            ),
        )
        first = ranked[0]
        row, key = first.row, first.key
        standing = found[row.account_id][_rank(found, row.account_id, row.document_id)]
        txns.append(
            {
                "id": key,
                "account_id": row.account_id,
                "posting_date": row.occurred_at.astimezone(zone).date(),
                "occurred_at": row.occurred_at,
                "amount": row.amount,
                "currency": first.code,
                "asset_identifier": row.asset_identifier,
                "description_norm": normalize_description(row.description_raw or ""),
                "counterparty": row.counterparty,
                "txid": row.txid,
                "from_address": row.from_address,
                "to_address": row.to_address,
                "origin": "document_line",
                "basis": standing.basis,
                "provisional": False,
                "movement_kind": FEE if row.line_kind == FEE else UNCLASSIFIED,
                "fee_type": row.fee_type,
                "trade_ref": row.trade_ref,
            }
        )
        for place, each in enumerate(ranked):
            sources.append(
                {
                    "txn_id": key,
                    "document_line_id": each.row.id,
                    "manual_ref": None,
                    "line_key_id": each.key,
                    "role": "primary" if place == 0 else "additional",
                }
            )
            if place and each.row.amount != row.amount:
                discrepancies.append(
                    {
                        "id": derived_id(
                            each.at.date(), ("source_discrepancy", each.row.id)
                        ),
                        "account_id": each.row.account_id,
                        "document_id": each.row.document_id,
                        "document_line_id": each.row.id,
                        "kind": QUANTITY_MISMATCH,
                    }
                )
    if txns:
        uow.execute(insert(txn), txns)
        uow.execute(insert(txn_source), sources)
    if discrepancies:
        uow.execute(insert(source_discrepancy), discrepancies)
    keyed = [each for lines in by_key.values() for each in lines]
    if keyed:
        uow.execute(
            update(document_line)
            .where(document_line.c.id == bindparam("b_id"))
            .values(
                description_norm=bindparam("b_norm"), line_key_id=bindparam("b_key")
            ),
            [
                {
                    "b_id": each.row.id,
                    "b_norm": normalize_description(each.row.description_raw or ""),
                    "b_key": each.key,
                }
                for each in keyed
            ],
        )
    return CryptoLines(
        txns=len(txns),
        additional_sources=len(sources) - len(txns),
        discrepancies=len(discrepancies),
        incomplete_lines=incomplete,
    )
