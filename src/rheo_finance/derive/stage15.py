"""Derivation stage 15, the payments: which ones there are, and what each was seen in
(FR-22, Key decision 18; SPEC Derivation stage 15, Money payments and Notices; edge
cases 15, 17, 34, 74 and 83). What a payment pays is the next step, and reads these
rows. This one reads no obligation, so it can run on either side of stage 14.

**A money line** is a fiat line, not excluded and not a fee, that left an account and
whose wording names a payee: the first in ``Payees`` order that a pattern of it names,
so one line is one payment however many payees name it. A document expense (the internal
account's txn of a receipt or a decided invoice) is named by its printed payee too.

**A card** (a payee standing for a ``credit_card`` account) is paid when money reaches
it: its money lines are the payment credits on that account (``transfer_like``, or the
parser's ``transfer_hint`` on a credit stage 8 took out of stage 9's reading, never a
refund). A debit elsewhere that names the card is one of three things:

- with an applied ``own_transfer`` edge, the other leg of the card's credit: never a
  payment of its own;
- with none, in a month coverage says the card has no lines for (``missing``,
  ``unparsed``, ``rejected``, or no row at all), the only evidence, so it is the
  payment;
- with none, in a month the card has lines for, a ``confirm_only`` payment ("payment to
  <card>?"), so an unpaired debit can neither apply twice nor vanish. A debit with no
  state (no transfer wording) is the same: nothing joined it.

A debit on the card itself that names the card (a returned payment) pays nothing.

**A payee standing for an account** is paid only as a card is, or as a vendor when
its account is a ``bill_vendor`` (an account that holds the vendor's bills and no
money). Words naming a payee that stands for any other account (chequing, savings, a
wallet) name nobody to pay: money sent there stays the owner's.

**One payment.** Lines joined by applied ``own_transfer`` edges are one payment when
one of them is a card's credit: that credit is its money line and the others are
``joined``. A group with no card credit is money moved between the owner's own
accounts, with no leg outside them, so it is no payment whatever payee a debit's
wording names. Any other money line is its own payment. Nothing here merges two lines
by amount or date, so two equal payments stay two.

**Reversed.** A line stage 8 (or the owner's ``pair``) made the original of an applied
``reversal`` edge is a ``reversed`` payment: it applies nothing and still takes its
notices. The reversal line itself, or any line with reversal wording left unpaired,
undoes something and is no payment. A line paired any other way (a conversion leg) is
none either.

**Notices.** A ``payment_notice`` line of a parsed document has no account. Its stream
is (the document's issuer, the line's ``source_type``); mail intake sets both. It is
keyed within its stream as a receipt is (``evidence.evidence_key``; copies of one notice
have one key, and the larger sha256 counts), and this stage stores the key on the line,
so a decision names it as ``finance.payment_notice:<key>``. Its payee is the first that
a pattern of it names in the printed ``counterparty``, else in the description. A notice
that names no payee and that no decision places makes no payment.

In order, each notice joining at most one payment:

1. The owner's ``pair`` and ``unpair`` on a (notice, line), the newest of each such
   couple holding. The newest ``pair`` of a notice joins it to the payment that line is
   evidence of, whatever the amount or date. A ``pair`` whose line is evidence of no
   payment is inert. An ``unpair`` bars that payment from steps 2 and 3.
2. Candidates: money payments of the notice's payee and currency, in any state, of an
   equal amount (``finance.payments.match_tolerance``) with dates within
   ``finance.payments.evidence_window_days``, taken by date gap, then payment id, then
   notice key. A payment takes at most one notice per stream.
3. A notice left over is **held** when such a money payment lies within
   ``finance.payments.notice_hold_days`` either side: a ``confirm_only`` payment of its
   own, ``notice_only``, so a bank line that posts late never makes a phantom credit.
   Otherwise it is an ``active`` notice-only payment, so what is owed drops when the
   email arrives before the statement. Leftovers are taken oldest first (then by key),
   and one is held too when an ``active`` notice-only payment of the same payee,
   currency and amount, from another stream, lies within the hold window: two emails
   about one payment count once (FR-22)."""

from __future__ import annotations

import datetime as dt
import uuid
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from functools import cache
from typing import Any, Final

from sqlalchemy import Row, bindparam, insert, select, update

from rheo_finance.context import UnitOfWork
from rheo_finance.decision import DecisionKind, reversed_clause
from rheo_finance.derive import fees, pairing, stage8
from rheo_finance.derive.evidence import DOCUMENT_ORIGIN, evidence_key
from rheo_finance.manual.accounts import BILL_VENDOR, CREDIT_CARD
from rheo_finance.manual.payees import Payee, Payees
from rheo_finance.manual.transfer_hints import TransferHintRow
from rheo_finance.reference.patterns import reversal_row_matches
from rheo_finance.storage import refs
from rheo_finance.storage.ids import derived_id
from rheo_finance.storage.schema import (
    account,
    coverage,
    decision,
    document,
    document_line,
    pair_edge,
    payee,
    payment,
    payment_evidence,
    txn,
    txn_source,
)
from rheo_finance.text import normalize_description

TOLERANCE_KEY: Final = "finance.payments.match_tolerance"
WINDOW_KEY: Final = "finance.payments.evidence_window_days"
HOLD_KEY: Final = "finance.payments.notice_hold_days"
NOTICE_KIND: Final = "payment_notice"
LINE_TYPE: Final = "txn"
OWN_TRANSFER: Final = "own_transfer"
REVERSAL: Final = "reversal"
# The coverage states of a month with no lines of the account behind it.
NO_LINES: Final = frozenset({"missing", "unparsed", "rejected"})
OTHER_NOTICE: Final = "other"

ACTIVE: Final = "active"
REVERSED: Final = "reversed"
CONFIRM_ONLY: Final = "confirm_only"
MONEY: Final = "money"
NOTICE_ONLY: Final = "notice_only"
JOINED: Final = "joined"
NOTICE: Final = "notice"


@dataclass(frozen=True)
class Windows:
    """What makes a notice and a payment one: the settings above."""

    tolerance: Decimal
    evidence_days: int
    hold_days: int


@dataclass(frozen=True)
class _Line:
    id: uuid.UUID
    account_id: uuid.UUID
    day: dt.date
    amount: Decimal
    currency: str
    # The primary source's wording first, so which payee a line names does not move.
    wordings: tuple[str, ...]
    # A card's payment credit, by the parser's wording or stage 9's reading.
    payment_wording: bool
    reversal_wording: bool
    pair_kind: str | None


@dataclass(frozen=True)
class _Notice:
    key: uuid.UUID
    line_id: uuid.UUID
    day: dt.date
    # Positive, as a payment's is: a notice may print either sign.
    amount: Decimal
    currency: str
    stream: str
    wordings: tuple[str, ...]


@dataclass
class _Payment:
    payee_id: uuid.UUID
    amount: Decimal
    currency: str
    paid_on: dt.date
    source: str
    state: str
    # What its id derives from: the money line's id, or the notice's key.
    basis: uuid.UUID
    # The ledger lines, the money line first, each with its role.
    lines: list[tuple[_Line, str]] = field(default_factory=list)
    # The notices that joined it, or its own.
    notices: list[_Notice] = field(default_factory=list)

    @property
    def id(self) -> uuid.UUID:
        return derived_id(self.paid_on, ("payment", self.basis))


@dataclass(frozen=True)
class _Payees:
    """The stored payees: ids by slug, the cards both ways, and the payees that stand
    for one of the owner's own money accounts."""

    by_slug: Mapping[str, uuid.UUID]
    of_card: Mapping[uuid.UUID, uuid.UUID]
    # The card account of each card payee.
    card_of: Mapping[uuid.UUID, uuid.UUID]
    # Payees standing for an account that is neither a card nor a bill vendor's.
    own_account: frozenset[uuid.UUID]


@dataclass(frozen=True)
class _Reversals:
    """The applied reversal edges: their originals, and the lines that undo them."""

    originals: frozenset[uuid.UUID]
    undoing: frozenset[uuid.UUID]


def _stored_payees(uow: UnitOfWork) -> _Payees:
    rows = uow.execute(
        select(
            payee.c.id, payee.c.slug, payee.c.account_id, account.c.kind
        ).select_from(payee.outerjoin(account, payee.c.account_id == account.c.id))
    ).all()
    cards = [row for row in rows if row.kind == CREDIT_CARD]
    return _Payees(
        by_slug={row.slug: row.id for row in rows},
        of_card={row.account_id: row.id for row in cards},
        card_of={row.id: row.account_id for row in cards},
        own_account=frozenset(
            row.id
            for row in rows
            if row.kind is not None and row.kind not in (CREDIT_CARD, BILL_VENDOR)
        ),
    )


def _line(
    group: Sequence[Row[Any]], hint_rows: Sequence[TransferHintRow]
) -> _Line | None:
    """One txn from its source lines, the primary first; a fee line is None."""
    first = group[0]
    if any(row.fee_type for row in group):
        return None
    wordings = [pairing.wording(row) for row in group]
    texts = [wordings[0], *sorted(set(wordings[1:]))]
    if first.origin == DOCUMENT_ORIGIN and first.counterparty:
        texts.append(first.counterparty)
    return _Line(
        id=first.id,
        account_id=first.account_id,
        day=first.posting_date,
        amount=first.amount,
        currency=first.currency,
        wordings=tuple(texts),
        payment_wording=bool(first.transfer_like)
        or any(row.transfer_hint for row in group),
        reversal_wording=any(
            row.reversal_hint or reversal_row_matches(text, hint_rows)
            for row, text in zip(group, wordings, strict=True)
        ),
        pair_kind=first.pair_kind,
    )


def _lines(
    uow: UnitOfWork, hint_rows: Sequence[TransferHintRow]
) -> dict[uuid.UUID, _Line]:
    """Every line a payment may rest on: fiat, nonzero, not excluded and not a fee."""
    rows = uow.execute(
        pairing.source_lines(
            txn.c.account_id,
            txn.c.posting_date,
            txn.c.amount,
            txn.c.currency,
            txn.c.counterparty,
            txn.c.origin,
            txn.c.transfer_like,
            txn.c.pair_kind,
            document_line.c.transfer_hint,
            document_line.c.reversal_hint,
            document_line.c.fee_type,
        ).where(*stage8.LINES, txn.c.origin != fees.FEE_ORIGIN)
    ).all()
    groups: dict[uuid.UUID, list[Row[Any]]] = defaultdict(list)
    for row in rows:
        groups[row.id].append(row)
    found = (_line(group, hint_rows) for group in groups.values())
    return {each.id: each for each in found if each is not None}


def _edges(uow: UnitOfWork, kind: str) -> list[tuple[uuid.UUID, uuid.UUID]]:
    """The applied edges of ``kind`` joining two lines, as (a, b)."""
    rows = uow.execute(
        select(pair_edge.c.a_txn_id, pair_edge.c.b_txn_id).where(
            pair_edge.c.kind == kind,
            pair_edge.c.applied,
            pair_edge.c.a_txn_id.is_not(None),
            pair_edge.c.b_txn_id.is_not(None),
        )
    )
    return [(row.a_txn_id, row.b_txn_id) for row in rows]


def _no_lines_months(uow: UnitOfWork) -> Callable[[uuid.UUID, dt.date], bool]:
    """Whether coverage says an account has no lines for a day's month."""
    months = {
        (row.account_id, row.month): row.state
        for row in uow.execute(
            select(coverage.c.account_id, coverage.c.month, coverage.c.state)
        )
    }

    def no_lines(account_id: uuid.UUID, day: dt.date) -> bool:
        state = months.get((account_id, day.replace(day=1)))
        return state is None or state in NO_LINES

    return no_lines


def _new_payment(
    money: _Line, others: Iterable[_Line], payee_id: uuid.UUID, state: str
) -> _Payment:
    """The payment of ``money`` and the lines joined to it. Its amount is what moved on
    the money line's side: a fan of credits to one debit is the sum of the credits."""
    joined = list(others)
    moved = sum(
        (
            each.amount
            for each in (money, *joined)
            if each.currency == money.currency
            and (each.amount > 0) == (money.amount > 0)
        ),
        Decimal(0),
    )
    return _Payment(
        payee_id=payee_id,
        amount=abs(moved),
        currency=money.currency,
        paid_on=money.day,
        source=MONEY,
        state=state,
        basis=money.id,
        lines=[(money, MONEY), *((each, JOINED) for each in joined)],
    )


class _Naming:
    """The payee a line's or a notice's words name, as a stored id. Each text is folded
    and searched once, because bank wording repeats. Words naming a payee that stands
    for an own money account name nobody to pay: that money stays the owner's."""

    def __init__(self, payees: Payees, stored: _Payees) -> None:
        self._naming: Callable[[str], Payee | None] = cache(payees.naming)
        self._stored = stored

    def __call__(self, wordings: Iterable[str]) -> uuid.UUID | None:
        for text in wordings:
            found = self._naming(text)
            if found is not None:
                named = self._stored.by_slug[found.slug]
                return None if named in self._stored.own_account else named
        return None


def _joined_payment(members: Sequence[_Line], stored: _Payees) -> _Payment | None:
    """The one payment of lines an applied ``own_transfer`` joins: money that reached
    a card. With no card credit among them every line is the owner's own, and the
    group is an own transfer, whatever payee a debit's wording names."""
    for line in members:
        if line.amount > 0 and line.account_id in stored.of_card:
            return _new_payment(
                line,
                [each for each in members if each is not line],
                stored.of_card[line.account_id],
                ACTIVE,
            )
    return None


def _single_payment(
    line: _Line,
    stored: _Payees,
    naming: _Naming,
    reversals: _Reversals,
    no_lines: Callable[[uuid.UUID, dt.date], bool],
) -> _Payment | None:
    """The payment of a line nothing joined, if it is one (module docstring)."""
    original = line.id in reversals.originals
    if not original and (
        line.id in reversals.undoing or line.reversal_wording or line.pair_kind
    ):
        return None
    state = REVERSED if original else ACTIVE
    if line.amount > 0:
        card = stored.of_card.get(line.account_id)
        if card is None or not line.payment_wording:
            return None
        return _new_payment(line, (), card, state)
    named = naming(line.wordings)
    if named is None:
        return None
    card_account = stored.card_of.get(named)
    if card_account == line.account_id:
        return None
    if (
        card_account is not None
        and state == ACTIVE
        and not no_lines(card_account, line.day)
    ):
        state = CONFIRM_ONLY
    return _new_payment(line, (), named, state)


def _money_payments(
    uow: UnitOfWork,
    lines: Mapping[uuid.UUID, _Line],
    stored: _Payees,
    naming: _Naming,
) -> list[_Payment]:
    joins = [edge for edge in _edges(uow, OWN_TRANSFER) if lines.keys() >= set(edge)]
    found: list[_Payment] = []
    joined: set[uuid.UUID] = set()
    for group in pairing.groups(joins, lambda edge: edge):
        ids = {end for edge in group for end in edge}
        joined |= ids
        members = sorted((lines[end] for end in ids), key=lambda x: (x.day, x.id))
        made = _joined_payment(members, stored)
        if made is not None:
            found.append(made)
    reversal_edges = _edges(uow, REVERSAL)
    reversals = _Reversals(
        originals=frozenset(b for _, b in reversal_edges),
        undoing=frozenset(a for a, _ in reversal_edges),
    )
    no_lines = _no_lines_months(uow)
    for line in lines.values():
        if line.id not in joined:
            made = _single_payment(line, stored, naming, reversals, no_lines)
            if made is not None:
                found.append(made)
    return found


def _notices(uow: UnitOfWork) -> list[_Notice]:
    """Every notice of a parsed document, the copy of the larger sha256 standing for its
    key, which every copy's line stores with its normalized description."""
    rows = uow.execute(
        select(
            document_line.c.id,
            document_line.c.document_id,
            document_line.c.posting_date,
            document_line.c.amount,
            document_line.c.currency,
            document_line.c.description_raw,
            document_line.c.counterparty,
            document_line.c.source_type,
            document_line.c.payment_ref_digest,
            document.c.issuer,
        )
        .join(document, document.c.id == document_line.c.document_id)
        .where(document.c.status == "parsed", document_line.c.line_kind == NOTICE_KIND)
        .order_by(document.c.sha256.desc(), document_line.c.line_no)
    ).all()
    occurrences: dict[uuid.UUID, Counter[tuple[object, ...]]] = defaultdict(Counter)
    found: dict[uuid.UUID, _Notice] = {}
    keyed: list[dict[str, object]] = []
    for row in rows:
        if not (row.posting_date and row.amount and row.currency):
            continue
        norm = normalize_description(row.description_raw or "")
        printed = normalize_description(row.counterparty or "")
        stream = f"notice:{row.issuer}:{row.source_type or OTHER_NOTICE}"
        natural = (
            stream, printed, row.posting_date, row.amount, row.currency, norm,
            row.payment_ref_digest or "",
        )  # fmt: skip
        seen = occurrences[row.document_id]
        seen[natural] += 1
        key = evidence_key(*natural, seen[natural])
        keyed.append({"b_id": row.id, "b_norm": norm, "b_key": key})
        found.setdefault(
            key,
            _Notice(
                key=key,
                line_id=row.id,
                day=row.posting_date,
                amount=abs(row.amount),
                currency=row.currency,
                stream=stream,
                wordings=(printed, norm),
            ),
        )
    if keyed:
        uow.execute(
            update(document_line)
            .where(document_line.c.id == bindparam("b_id"))
            .values(
                description_norm=bindparam("b_norm"), line_key_id=bindparam("b_key")
            ),
            keyed,
        )
    return list(found.values())


def _notice_decisions(
    uow: UnitOfWork,
) -> tuple[Mapping[uuid.UUID, uuid.UUID], Mapping[uuid.UUID, set[uuid.UUID]]]:
    """The owner's decisions on a (notice, line), by notice key: the txn a ``pair``
    joins it to, and the txns an ``unpair`` keeps it from (module docstring)."""
    rows = uow.execute(
        select(decision.c.kind, decision.c.target_ref, decision.c.counterpart_ref)
        .where(
            decision.c.kind.in_([DecisionKind.PAIR.value, DecisionKind.UNPAIR.value]),
            decision.c.counterpart_ref.is_not(None),
            ~reversed_clause(decision),
        )
        .order_by(decision.c.decided_at.desc(), decision.c.id.desc())
    ).all()
    named: list[tuple[str, uuid.UUID, uuid.UUID]] = []
    for row in rows:
        by_type = {
            ref.type: uuid.UUID(ref.id)
            for ref in map(refs.parse, (row.target_ref, row.counterpart_ref))
            if ref.system == refs.FINANCE_SYSTEM
            and ref.type in (NOTICE_KIND, LINE_TYPE)
        }
        if NOTICE_KIND in by_type and LINE_TYPE in by_type:
            named.append((row.kind, by_type[NOTICE_KIND], by_type[LINE_TYPE]))
    if not named:
        return {}, {}
    txn_of = {
        row.line_key_id: row.txn_id
        for row in uow.execute(
            select(txn_source.c.line_key_id, txn_source.c.txn_id)
            .distinct()
            .where(
                txn_source.c.line_key_id.in_({key for _, _, key in named}),
                txn_source.c.role.in_(pairing.LINE_ROLES),
            )
        )
    }
    seen: set[tuple[uuid.UUID, uuid.UUID]] = set()
    paired: dict[uuid.UUID, uuid.UUID] = {}
    unpaired: dict[uuid.UUID, set[uuid.UUID]] = defaultdict(set)
    for kind, notice, key in named:
        line = txn_of.get(key)
        if line is None or (notice, line) in seen:
            continue
        seen.add((notice, line))
        if kind == DecisionKind.UNPAIR.value:
            unpaired[notice].add(line)
        else:
            paired.setdefault(notice, line)
    return paired, unpaired


def _join_notices(
    uow: UnitOfWork,
    notices: Sequence[_Notice],
    money: Sequence[_Payment],
    naming: _Naming,
    windows: Windows,
) -> list[_Payment]:
    """Join each notice to a money payment, in the order of the module docstring, and
    return a payment for each notice left over."""
    paired, unpaired = _notice_decisions(uow)
    of_line = {line.id: each for each in money for line, _ in each.lines}
    streams: dict[uuid.UUID, set[str]] = defaultdict(set)
    placed: set[uuid.UUID] = set()
    for notice in notices:
        line = paired.get(notice.key)
        target = None if line is None else of_line.get(line)
        if target is not None:
            target.notices.append(notice)
            streams[target.basis].add(notice.stream)
            placed.add(notice.key)

    def near(notice: _Notice, payee_id: uuid.UUID, days: int) -> list[_Payment]:
        """The money payments that could be ``notice``: not barred by an ``unpair``."""
        barred = unpaired.get(notice.key, set())
        return [
            each
            for each in money
            if each.payee_id == payee_id
            and each.currency == notice.currency
            and abs(each.amount - notice.amount) <= windows.tolerance
            and abs((each.paid_on - notice.day).days) <= days
            and not any(line.id in barred for line, _ in each.lines)
        ]

    payee_of = {
        notice.key: found
        for notice in notices
        if notice.key not in placed and (found := naming(notice.wordings)) is not None
    }
    candidates = sorted(
        (
            (abs((each.paid_on - notice.day).days), each.id, notice.key, each, notice)
            for notice in notices
            if notice.key in payee_of
            for each in near(notice, payee_of[notice.key], windows.evidence_days)
        ),
        key=lambda found: found[:3],
    )
    for _, _, key, each, notice in candidates:
        if key not in placed and notice.stream not in streams[each.basis]:
            each.notices.append(notice)
            streams[each.basis].add(notice.stream)
            placed.add(key)

    def told_already(notice: _Notice, payee_id: uuid.UUID, own: list[_Payment]) -> bool:
        """An earlier leftover notice of another stream stands as this payment."""
        return any(
            each.state == ACTIVE
            and each.payee_id == payee_id
            and each.currency == notice.currency
            and abs(each.amount - notice.amount) <= windows.tolerance
            and abs((each.paid_on - notice.day).days) <= windows.hold_days
            and each.notices[0].stream != notice.stream
            for each in own
        )

    own: list[_Payment] = []
    for notice in sorted(notices, key=lambda each: (each.day, each.key)):
        if notice.key in placed or notice.key not in payee_of:
            continue
        payee_id = payee_of[notice.key]
        held = near(notice, payee_id, windows.hold_days) or told_already(
            notice, payee_id, own
        )
        own.append(
            _Payment(
                payee_id=payee_id,
                amount=notice.amount,
                currency=notice.currency,
                paid_on=notice.day,
                source=NOTICE_ONLY,
                state=CONFIRM_ONLY if held else ACTIVE,
                basis=notice.key,
                notices=[notice],
            )
        )
    return own


def _evidence_rows(each: _Payment) -> list[dict[str, object]]:
    def row(
        seen: uuid.UUID,
        stream: str,
        role: str,
        txn_id: uuid.UUID | None = None,
        line_id: uuid.UUID | None = None,
    ) -> dict[str, object]:
        return {
            "id": derived_id(each.paid_on, ("payment_evidence", each.id, seen)),
            "payment_id": each.id,
            "stream_key": stream,
            "role": role,
            "txn_id": txn_id,
            "document_line_id": line_id,
        }

    return [
        *(
            row(line.id, f"account:{line.account_id}", role, txn_id=line.id)
            for line, role in each.lines
        ),
        *(
            row(notice.key, notice.stream, NOTICE, line_id=notice.line_id)
            for notice in each.notices
        ),
    ]


def build_payments(
    uow: UnitOfWork,
    payees: Payees,
    hint_rows: Sequence[TransferHintRow],
    windows: Windows,
) -> int:
    """Write every payment and its evidence (module docstring); how many it made. The
    caller cleared the tables."""
    stored = _stored_payees(uow)
    naming = _Naming(payees, stored)
    money = _money_payments(uow, _lines(uow, hint_rows), stored, naming)
    own = _join_notices(uow, _notices(uow), money, naming, windows)
    made = [*money, *own]
    if made:
        uow.execute(
            insert(payment),
            [
                {
                    "id": each.id,
                    "payee_id": each.payee_id,
                    "amount": each.amount,
                    "currency": each.currency,
                    "paid_on": each.paid_on,
                    "source": each.source,
                    "state": each.state,
                }
                for each in made
            ],
        )
        uow.execute(
            insert(payment_evidence),
            [row for each in made for row in _evidence_rows(each)],
        )
    return len(made)
