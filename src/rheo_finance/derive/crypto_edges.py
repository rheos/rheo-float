"""Derivation stage 9 for crypto: own-wallet transfers, two-sided and one-sided, as
candidate edges (FR-15(b), edge cases 24, 39 and 77; SPEC Derivation stage 9).

Like the fiat part of stage 9 (``stage9``), this stores candidate edges in
``pair_edge`` with ``applied`` false, and stage 11 decides which apply, counting the
edges of every kind on a line together.

**Which lines.** Crypto txns of kind ``crypto_transfer`` or ``crypto_unclassified``
(stage 7) with a nonzero quantity, a time and no exclusion, and no
``crypto_labels.csv`` label (``labelled``): a labelled line is the owner's to account
for and starts or ends no pairing. A fee line (``crypto_fee``) and a swap leg are never
an end. Reading a line reads its primary source (``pairing.source_lines``) for
``fee_stated``: the primary's figure is the txn's.

**``own_transfer``.** A withdrawal of kind ``crypto_transfer`` (transfer-like) to a
deposit of the same asset code on another account:

- with a TXID on both sides, only when the TXIDs are equal (``txid_key``: a hex TXID
  compared in lower case, without a leading ``0x``); the TXID settles it, whatever
  the times or the figures;
- otherwise when the deposit's quantity equals the withdrawal's within
  ``finance.crypto.qty_epsilon``, or, when the withdrawal's line has ``fee_stated``
  false (one printed figure that may include an unstated fee), when the deposit is
  smaller by at most ``finance.crypto.fee_tolerance_pct`` percent of the withdrawal
  (a fee only ever lowers what arrives; a deposit line states no fee of its own), and
  the two times are at most ``finance.crypto.transfer_window_hours`` apart;
- when the withdrawal's destination is an ``own_address`` (compared as
  ``manual.own_addresses.normalize_address`` holds it), only on that address's
  account.

``a_txn_id`` is the withdrawal, ``b_txn_id`` the deposit. An edge's id is derived on
the UTC date of the withdrawal's time, never its local posting date. A crypto pair
books no derived fee line (stage 11 makes those for fiat legs only).

**``own_one_sided``** (edge case 77). A withdrawal whose destination is an own address
of account X and that has no ``own_transfer`` candidate (X's rows are not loaded, or
the receiving row is missing) gets one edge with ``a_txn_id`` the withdrawal, no
``b_txn_id`` and ``b_account_id`` X. When it is the line's only edge stage 11 applies
it: the line is ``paired`` with ``pair_kind = own_one_sided``, in no total and no
disposal, and raises no ``finance.withdrawal.flagged``. When the receiving row loads,
the next derive finds the ``own_transfer`` candidate instead and the line pairs two
sided, with no decision.
"""

from __future__ import annotations

import datetime as dt
import re
import uuid
from dataclasses import dataclass
from decimal import Decimal
from typing import Final

from sqlalchemy import and_, insert, select

from rheo_finance.context import UnitOfWork
from rheo_finance.derive import pairing
from rheo_finance.manual.own_addresses import normalize_address
from rheo_finance.storage.ids import derived_id
from rheo_finance.storage.schema import (
    crypto_label,
    document_line,
    own_address,
    pair_edge,
    txn,
    txn_source,
)

WINDOW_KEY: Final = "finance.crypto.transfer_window_hours"
FEE_PCT_KEY: Final = "finance.crypto.fee_tolerance_pct"
EPSILON_KEY: Final = "finance.crypto.qty_epsilon"
TRANSFER: Final = "crypto_transfer"
UNCLASSIFIED: Final = "crypto_unclassified"
FIAT: Final = "fiat"
KIND: Final = "own_transfer"
ONE_SIDED: Final = "own_one_sided"
HUNDRED: Final = Decimal(100)
_HEX: Final = re.compile(r"(0[xX])?[0-9a-fA-F]+")


@dataclass(frozen=True)
class Tolerance:
    """The crypto transfer settings."""

    window_hours: int
    fee_pct: Decimal
    epsilon: Decimal


@dataclass(frozen=True)
class _Line:
    id: uuid.UUID
    account_id: uuid.UUID
    code: str
    amount: Decimal
    at: dt.datetime
    # The UTC date of ``at``: edge ids are keyed to it, as line ids are (stage 3), so
    # a change of ``finance.locale.timezone`` re-keys no edge.
    day: dt.date
    txid: str | None
    to_address: str | None
    kind: str
    fee_stated: bool


def txid_key(txid: str) -> str:
    """A TXID in its compared form: stripped, and when it is hex, without a leading
    ``0x`` (an EVM TXID is printed with and without it) and lower-cased."""
    txid = txid.strip()
    if not _HEX.fullmatch(txid):
        return txid
    return txid[2:].lower() if txid[:2] in ("0x", "0X") else txid.lower()


def labelled(uow: UnitOfWork) -> set[uuid.UUID]:
    """The crypto txns a ``crypto_labels.csv`` row names, through the line key it
    holds (a primary or an additional source's)."""
    return set(
        uow.scalars(
            select(txn.c.id)
            .distinct()
            .join(
                txn_source,
                and_(
                    txn_source.c.txn_id == txn.c.id,
                    txn_source.c.role.in_(pairing.LINE_ROLES),
                ),
            )
            .join(crypto_label, crypto_label.c.line_key_id == txn_source.c.line_key_id)
            .where(txn.c.movement_kind != FIAT)
        )
    )


def _lines(uow: UnitOfWork) -> list[_Line]:
    rows = uow.execute(
        pairing.source_lines(
            txn.c.account_id,
            txn.c.currency,
            txn.c.amount,
            txn.c.occurred_at,
            txn.c.txid,
            txn.c.to_address,
            txn.c.movement_kind,
            document_line.c.fee_stated,
        ).where(
            txn.c.movement_kind.in_((TRANSFER, UNCLASSIFIED)),
            txn.c.amount != 0,
            txn.c.occurred_at.is_not(None),
            txn.c.exclusion.is_(None),
        )
    )
    skip = labelled(uow)
    found: dict[uuid.UUID, _Line] = {}
    for row in rows:
        # The primary source comes first (``pairing.source_lines``): its figure is
        # the txn's, so its ``fee_stated`` is the one that counts.
        if row.id in found or row.id in skip:
            continue
        found[row.id] = _Line(
            id=row.id,
            account_id=row.account_id,
            code=row.currency,
            amount=row.amount,
            at=row.occurred_at,
            day=row.occurred_at.astimezone(dt.UTC).date(),
            txid=row.txid,
            to_address=row.to_address,
            kind=row.movement_kind,
            fee_stated=bool(row.fee_stated),
        )
    return sorted(found.values(), key=lambda line: (line.day, line.id))


def _matches(out: _Line, into: _Line, tolerance: Tolerance) -> bool:
    if out.txid is not None and into.txid is not None:
        return txid_key(out.txid) == txid_key(into.txid)
    window = dt.timedelta(hours=tolerance.window_hours)
    if abs(into.at - out.at) > window:
        return False
    sent = -out.amount
    short = sent - into.amount
    if abs(short) <= tolerance.epsilon:
        return True
    return not out.fee_stated and 0 < short <= sent * tolerance.fee_pct / HUNDRED


def pair_crypto(uow: UnitOfWork, *, tolerance: Tolerance) -> None:
    """Store every crypto ``own_transfer`` and ``own_one_sided`` candidate edge
    (module docstring)."""
    lines = _lines(uow)
    owners = {
        address: account_id
        for address, account_id in uow.execute(
            select(own_address.c.address, own_address.c.account_id)
        )
    }
    deposits: dict[str, list[_Line]] = {}
    for line in lines:
        if line.amount > 0:
            deposits.setdefault(line.code, []).append(line)
    edges: list[dict[str, object]] = []
    for out in lines:
        if out.amount > 0 or out.kind != TRANSFER:
            continue
        destination = (
            None
            if out.to_address is None
            else owners.get(normalize_address(out.to_address))
        )
        found = [
            into
            for into in deposits.get(out.code, ())
            if into.account_id != out.account_id
            and (destination is None or into.account_id == destination)
            and _matches(out, into, tolerance)
        ]
        edges += [
            {
                "id": derived_id(out.day, ("pair_edge", KIND, out.id, into.id)),
                "kind": KIND,
                "a_txn_id": out.id,
                "b_txn_id": into.id,
                "applied": False,
            }
            for into in found
        ]
        if not found and destination is not None:
            edges.append(
                {
                    "id": derived_id(out.day, ("pair_edge", ONE_SIDED, out.id)),
                    "kind": ONE_SIDED,
                    "a_txn_id": out.id,
                    "b_txn_id": None,
                    "b_account_id": destination,
                    "applied": False,
                }
            )
    if edges:
        uow.execute(
            insert(pair_edge),
            [{"b_account_id": None, **edge} for edge in edges],
        )
