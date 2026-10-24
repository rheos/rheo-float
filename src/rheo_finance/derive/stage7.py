"""Derivation stage 7, attribution and exclusions (core part): the exclusions and the
crypto movement kinds (FR-15(b), FR-41, FR-63; SPEC Derivation stage 7).

The peer-money attribution of the spec's stage 7 is a feature call
(``features/peer_money``) that does not exist yet, so it has no call site here.

**Exclusions.** Every line on an account marked ``placeholder`` gets
``exclusion = placeholder``. A received crypto line (a crypto txn with a positive
quantity) on any other account gets ``exclusion = spam`` when one of its lines (its
primary or an additional source) flags its coin ``is_fake``, or when a line states its
CAD worth as 0 and its account has no outgoing line (a negative quantity, a fee
included) of the same asset identifier: a token nobody ever sent from the account is
an airdrop of nothing. An unreversed ``include`` decision naming the line clears
either exclusion. An excluded line is in no total and starts no pairing.

**Movement kinds**, on every crypto txn (stage 3 gave each ``crypto_fee`` or
``crypto_unclassified``); a fee line stays ``crypto_fee``. Of the rest:

- lines sharing a ``trade_ref`` on one account, with both signs among them, are
  ``crypto_swap_leg``: the two sides of one swap;
- otherwise a single-direction transfer line is ``crypto_transfer`` and transfer-like
  (stage 11 then gives it a state, ``unresolved`` until an edge pairs it): a line
  whose source marks it a transfer (the parser's ``transfer_hint``: an exchange
  deposit or withdrawal, a tracker's sent or received row), or whose counterpart
  address is an ``own_address`` (the destination of a withdrawal, the origin of a
  deposit, each compared as ``manual.own_addresses.normalize_address`` holds it). An
  excluded line keeps its kind but is not transfer-like, so it never sits in the
  unresolved count;
- everything else stays ``crypto_unclassified``: listed as an unclassified crypto
  movement, in no total, and never a cost basis (FR-41). ``trading`` lines arrive with
  ``features/trading`` (Phase 8).
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from typing import Final

from sqlalchemy import and_, bindparam, select, update

from rheo_finance.context import UnitOfWork
from rheo_finance.decision import DecisionKind, reversed_clause
from rheo_finance.derive import pairing
from rheo_finance.manual.own_addresses import normalize_address
from rheo_finance.parsers.base import (
    CRYPTO_FEE,
    CRYPTO_SWAP_LEG,
    CRYPTO_TRANSFER,
    CRYPTO_UNCLASSIFIED,
    FIAT,
)
from rheo_finance.storage import refs
from rheo_finance.storage.schema import (
    account,
    decision,
    document_line,
    own_address,
    txn,
    txn_source,
)

PLACEHOLDER: Final = "placeholder"
SPAM: Final = "spam"


def _included(uow: UnitOfWork) -> set[uuid.UUID]:
    """The txns an unreversed ``include`` decision names, through the line key it
    holds (a primary or an additional source's)."""
    targets = uow.scalars(
        select(decision.c.target_ref).where(
            decision.c.kind == DecisionKind.INCLUDE.value, ~reversed_clause(decision)
        )
    ).all()
    keys = set()
    for target in targets:
        parsed = refs.parse(target)
        if parsed.system == refs.FINANCE_SYSTEM and parsed.type == "txn":
            keys.add(uuid.UUID(parsed.id))
    if not keys:
        return set()
    return set(
        uow.scalars(
            select(txn_source.c.txn_id).where(
                txn_source.c.line_key_id.in_(keys),
                txn_source.c.role.in_(pairing.LINE_ROLES),
            )
        )
    )


def _exclusions(uow: UnitOfWork) -> dict[uuid.UUID, str]:
    placeholders = set(
        uow.scalars(
            select(txn.c.id)
            .join(account, account.c.id == txn.c.account_id)
            .where(account.c.placeholder)
        )
    )
    found = dict.fromkeys(placeholders, PLACEHOLDER)
    crypto = uow.execute(
        select(
            txn.c.id,
            txn.c.account_id,
            txn.c.asset_identifier,
            txn.c.amount,
            document_line.c.is_fake,
            document_line.c.stated_cad,
        )
        .select_from(txn)
        .outerjoin(
            txn_source,
            and_(
                txn_source.c.txn_id == txn.c.id,
                txn_source.c.role.in_(pairing.LINE_ROLES),
            ),
        )
        .outerjoin(document_line, document_line.c.id == txn_source.c.document_line_id)
        .where(txn.c.movement_kind != FIAT)
    ).all()
    sent = {(row.account_id, row.asset_identifier) for row in crypto if row.amount < 0}
    fake: set[uuid.UUID] = set()
    worthless: set[uuid.UUID] = set()
    for row in crypto:
        if row.amount <= 0 or row.id in placeholders:
            continue
        if row.is_fake:
            fake.add(row.id)
        elif row.stated_cad == 0 and (row.account_id, row.asset_identifier) not in sent:
            worthless.add(row.id)
    found.update(dict.fromkeys(fake | worthless, SPAM))
    for line_id in _included(uow):
        found.pop(line_id, None)
    return found


def _movement_kinds(
    uow: UnitOfWork, excluded: set[uuid.UUID]
) -> dict[uuid.UUID, tuple[str, bool]]:
    """Each non-fee crypto txn's (movement kind, transfer-like)."""
    own = set(uow.scalars(select(own_address.c.address)))
    rows = uow.execute(
        select(
            txn.c.id,
            txn.c.account_id,
            txn.c.amount,
            txn.c.trade_ref,
            txn.c.from_address,
            txn.c.to_address,
            document_line.c.transfer_hint,
        )
        .select_from(txn)
        .outerjoin(
            txn_source,
            and_(
                txn_source.c.txn_id == txn.c.id,
                txn_source.c.role.in_(pairing.LINE_ROLES),
            ),
        )
        .outerjoin(document_line, document_line.c.id == txn_source.c.document_line_id)
        .where(txn.c.movement_kind.not_in((FIAT, CRYPTO_FEE)))
    ).all()
    signs: dict[tuple[uuid.UUID, str], set[bool]] = defaultdict(set)
    hinted: set[uuid.UUID] = set()
    lines: dict[uuid.UUID, tuple[uuid.UUID, str | None, str | None]] = {}
    for row in rows:
        if row.transfer_hint:
            hinted.add(row.id)
        counterpart = row.to_address if row.amount < 0 else row.from_address
        lines[row.id] = (row.account_id, row.trade_ref, counterpart)
        if row.trade_ref is not None:
            signs[row.account_id, row.trade_ref].add(row.amount > 0)
    kinds: dict[uuid.UUID, tuple[str, bool]] = {}
    for line_id, (account_id, trade_ref, counterpart) in lines.items():
        if trade_ref is not None and len(signs[account_id, trade_ref]) == 2:
            kinds[line_id] = (CRYPTO_SWAP_LEG, False)
        elif line_id in hinted or (
            counterpart is not None and normalize_address(counterpart) in own
        ):
            kinds[line_id] = (CRYPTO_TRANSFER, line_id not in excluded)
        else:
            kinds[line_id] = (CRYPTO_UNCLASSIFIED, False)
    return kinds


def attribute(uow: UnitOfWork) -> None:
    """Set ``exclusion`` on every txn, and ``movement_kind`` and ``transfer_like`` on
    the crypto txns (module docstring)."""
    exclusions = _exclusions(uow)
    if exclusions:
        uow.execute(
            update(txn)
            .where(txn.c.id == bindparam("b_id"))
            .values(exclusion=bindparam("b_exclusion")),
            [{"b_id": key, "b_exclusion": value} for key, value in exclusions.items()],
        )
    kinds = _movement_kinds(uow, set(exclusions))
    if kinds:
        uow.execute(
            update(txn)
            .where(txn.c.id == bindparam("b_id"))
            .values(
                movement_kind=bindparam("b_kind"), transfer_like=bindparam("b_like")
            ),
            [
                {"b_id": key, "b_kind": kind, "b_like": like}
                for key, (kind, like) in kinds.items()
            ],
        )
