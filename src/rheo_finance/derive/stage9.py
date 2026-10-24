"""Derivation stage 9, fiat own transfers and same-account currency conversions: the
candidate edges (FR-15, FR-15(c), Key decision 9, edge cases 4, 5 and 69).

Stage 9 stores every candidate ``own_transfer`` and ``conversion`` edge in
``pair_edge`` with ``applied`` false; stage 11 decides which apply. On the lines it
reads it also sets ``txn.transfer_like`` and ``txn.hint_account_id``.

**Which lines.** Fiat txns with a nonzero amount, never a line stage 8 paired as a
reversal (``pair_kind = reversal``), an excluded line, or a party line. Reading a line
means reading its primary and additional source lines (never an evidence source, such
as a receipt); a manual cash line, with no document line, has the txn's own wording.

**Transfer-like** (the lines that start a pairing): a source line carries the parser's
``transfer_hint`` or ``conversion_hint``, or its wording matches an ``out``, ``in`` or
``conversion`` row of ``transfer_hints.csv`` (``reference.patterns.row_hints``, the
matching ``classify`` uses). A fee line (``fee_type`` set) never is: a parser prints a
transfer's fee as its own line under the transfer's wording plus "(fee)", and the fee
is spending, not a leg.

**The hint account.** A line's ``hint_account_slug`` names its counterpart in two
ways, told apart by the transfer kind (prompt 29): a built-in pattern's counterpart is
an issuer key of the archive's issuer map, which resolves to every account that lines
of that issuer's documents are on; a ``transfer_hints.csv`` row's (kind ``manual_out``
or ``manual_in``) is the owner's own account slug, which resolves to that account. A
parser's hint wins over a row on the same source line, as in ``classify``, and the
first source line (the primary first) that names a counterpart gives the txn's.
``hint_account_id`` is set when the hint resolves to exactly one account.

**Candidates.** An edge joins a transfer-like line (the start) to a line of the
opposite sign on another account, posted at most ``finance.transfers.window_days``
apart, when:

- the start's hint, when it resolved, is a preference (the search looks on that
  account first): the start's candidates are the lines on the hinted account or
  accounts when any qualifies there, else the lines on every own account. The other
  end's hint is no filter: it acts only when that line is itself a start, so a card
  whose payment wording names one bank still pairs with a payment from another;
- on a card account (kind ``credit_card``, whose parsers mark the payment credits with
  a payment hint) the card-side line is itself transfer-like, so a purchase refund of
  the same amount is never offered as the other leg of a chequing debit;
- a fee line (``fee_type`` set) is neither end: it never starts a pairing (above), and
  it is never the other end either, so a printed fee is never paired away by a small
  transfer-like line of about its size and always counts once as spending;
- a line with a ``conversion`` candidate edge (below), applied or not, is neither
  end: its wording and its one account settle it as a conversion leg, so the in-leg
  of a payment-service currency purchase is never offered to the bank pull that
  funded the purchase, and the pull pairs with the deposit alone;
- the two legs net to zero within the tolerance: the larger of
  ``finance.transfers.fee_tolerance_abs`` (CAD) and ``fee_tolerance_pct`` percent of
  the larger leg's CAD value (stage 4). A pair outside it gets no edge.

**Unconverted legs.** Two legs in one currency compare natively: the net is the sum of
the amounts, and the CAD tolerance is carried into that currency at the larger leg's
own CAD value (exact for CAD). When the larger leg has no CAD value, the percent part
alone applies, since the absolute part is a CAD figure. Legs in two currencies compare
by their CAD values; when either is ``unconverted`` there is no edge, because nothing
can be measured.

**Equal card payments.** For each group of lines the edges connect: when every debit
is a non-card line, every credit a card-side line, all of one amount and currency,
the debits and the credits are the same number (two or more), every debit has every
credit as a candidate (the sets are mutual), and no two debits and no two credits share
a posting date, the edges thin to the date order: the k-th debit by posting date to the
k-th credit. Each kept edge was a candidate, so it lies inside the window. Any other
group keeps every edge. The result depends on the two date-sorted lists only, never on
the order lines were read.

**Conversions** (FR-15(c)). A ``conversion`` edge joins two lines on the SAME account,
opposite in sign, in two currencies, both carrying conversion wording (the parser's
``conversion_hint`` or a ``conversion`` row of ``transfer_hints.csv``; a fee line never
is a leg), posted at most ``finance.conversions.window_days`` apart. ``a_txn_id`` is
the out-leg (the debit), ``b_txn_id`` the in-leg, and the conversion date is the
out-leg's posting date. Lines on two accounts are never a conversion: the
``own_transfer`` rule above handles them, since conversion wording is transfer-like.

- The spread (``spread_cad``) is the out-leg's CAD value (stage 4) minus the in-leg's
  at the stored Bank of Canada rate of the conversion date (a CAD in-leg is its own
  amount). The parsers that print a conversion fee (the money-transfer history and
  statement CSV, the payment-service activity PDF and CSV) emit the out-leg NET of it
  and the fee as its own line, so this spread is already the residual after the
  printed fee; taking the fee off again would count it twice.
- The edge exists only when the spread is inside
  ``finance.conversions.spread_tolerance_pct`` percent of the out-leg's CAD value, in
  either direction (the transfer tolerance is never used here). Outside it there is
  no edge, so the legs end ``unresolved`` unless another edge pairs them.
- With a leg unmeasurable (the out-leg ``unconverted``, or no stored rate for the
  in-leg on the conversion date) the wording settles the pair: the edge is stored with
  no spread, and stage 11 books no fee for it until a rate exists. Every foreign leg
  with no rate stays in the unconverted count meanwhile; a CAD leg is native and never
  unconverted (FR-14), so a CAD to USD conversion with no rate counts one line.
- The issuer's printed fee is a fee line on the account, posted on a leg's date,
  whose wording is a leg's wording plus `` (fee)``, as the parsers print it. Stage 9
  names it (the earliest, when several match) as the edge's ``fee_txn_id``, so stage
  11 books no second fee line for that conversion. Two conversions with the same
  wording on one account and day can name the same line; stage 11 then lets neither
  keep it.
"""

from __future__ import annotations

import bisect
import dataclasses
import datetime as dt
import uuid
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Final

from sqlalchemy import bindparam, insert, select, update

from rheo_finance.context import UnitOfWork
from rheo_finance.derive import pairing
from rheo_finance.derive.stage4 import cents
from rheo_finance.manual.transfer_hints import TransferHintRow
from rheo_finance.parsers.base import FIAT
from rheo_finance.rates import QUOTE_CAD, RateBook, rate_book
from rheo_finance.reference.patterns import row_hints
from rheo_finance.storage.ids import derived_id
from rheo_finance.storage.schema import (
    account,
    document,
    document_line,
    pair_edge,
    txn,
)

WINDOW_KEY: Final = "finance.transfers.window_days"
ABS_KEY: Final = "finance.transfers.fee_tolerance_abs"
PCT_KEY: Final = "finance.transfers.fee_tolerance_pct"
KIND: Final = "own_transfer"
CONVERSION_WINDOW_KEY: Final = "finance.conversions.window_days"
SPREAD_KEY: Final = "finance.conversions.spread_tolerance_pct"
CONVERSION: Final = "conversion"
# A parser prints a fee under its transaction's wording plus this.
FEE_SUFFIX: Final = " (fee)"
CARD: Final = "credit_card"
# The transfer kind of a ``transfer_hints.csv`` row: its counterpart is an account slug.
MANUAL_PREFIX: Final = "manual_"
# The kind recorded for a parser's hint, whose counterpart is an issuer key.
PATTERN: Final = "pattern"
HUNDRED: Final = Decimal(100)


@dataclass(frozen=True)
class Tolerance:
    window_days: int
    abs_cad: Decimal
    pct: Decimal


@dataclass(frozen=True)
class Spread:
    """The conversion settings: the legs' window and the spread tolerance."""

    window_days: int
    pct: Decimal


@dataclass(frozen=True)
class _Line:
    id: uuid.UUID
    account_id: uuid.UUID
    card: bool
    currency: str
    amount: Decimal
    cad: Decimal | None
    day: dt.date
    transfer_like: bool
    # (transfer kind, counterpart) of the first source line naming one.
    hint: tuple[str, str] | None
    # A conversion leg: a source line carries conversion wording, and it is no fee.
    conversion: bool = False
    # A fee line (``fee_type`` set), printed by its parser.
    fee: bool = False
    # The normalized wording of every source line.
    wordings: frozenset[str] = frozenset()


@dataclass
class _Reading:
    """One txn's source lines, folded as they are read."""

    line: _Line
    transfer_like: bool = False
    conversion: bool = False
    hint: tuple[str, str] | None = None
    wordings: set[str] = dataclasses.field(default_factory=set)


def _lines(uow: UnitOfWork, hint_rows: Sequence[TransferHintRow]) -> list[_Line]:
    cards = set(uow.scalars(select(account.c.id).where(account.c.kind == CARD)))
    rows = uow.execute(
        pairing.source_lines(
            txn.c.account_id,
            txn.c.currency,
            txn.c.amount,
            txn.c.cad_amount,
            txn.c.posting_date,
            txn.c.fee_type,
            document_line.c.transfer_hint,
            document_line.c.conversion_hint,
            document_line.c.hint_account_slug,
        ).where(
            txn.c.movement_kind == FIAT,
            txn.c.amount != 0,
            txn.c.exclusion.is_(None),
            txn.c.party_id.is_(None),
            txn.c.pair_kind.is_distinct_from("reversal"),
        )
    )
    found: dict[uuid.UUID, _Reading] = {}
    fees: set[uuid.UUID] = set()
    for row in rows:
        if row.fee_type is not None:
            fees.add(row.id)
        reading = found.get(row.id)
        if reading is None:
            reading = found[row.id] = _Reading(
                _Line(
                    id=row.id,
                    account_id=row.account_id,
                    card=row.account_id in cards,
                    currency=row.currency,
                    amount=row.amount,
                    cad=row.cad_amount,
                    day=row.posting_date,
                    transfer_like=False,
                    hint=None,
                )
            )
        # A source with no document line has no parser hints (all null).
        parser_transfer = bool(row.transfer_hint)
        parser_conversion = bool(row.conversion_hint)
        said = pairing.wording(row)
        reading.wordings.add(said)
        rows_say = row_hints(said, hint_rows)
        reading.conversion = (
            reading.conversion or parser_conversion or rows_say.conversion_hint
        )
        reading.transfer_like = reading.transfer_like or any(
            (
                parser_transfer,
                parser_conversion,
                rows_say.transfer_hint is not None,
                rows_say.conversion_hint,
            )
        )
        # The primary source is read first (``pairing.source_lines``), so its
        # counterpart wins.
        if reading.hint is None:
            if parser_transfer:
                if row.hint_account_slug is not None:
                    reading.hint = (PATTERN, row.hint_account_slug)
            elif rows_say.transfer_hint is not None and rows_say.hint_account_slug:
                reading.hint = (rows_say.transfer_hint, rows_say.hint_account_slug)
    return [
        dataclasses.replace(
            each.line,
            transfer_like=each.transfer_like and each.line.id not in fees,
            hint=each.hint,
            conversion=each.conversion and each.line.id not in fees,
            fee=each.line.id in fees,
            wordings=frozenset(each.wordings),
        )
        for each in found.values()
    ]


def _resolver(uow: UnitOfWork) -> Mapping[tuple[str, str], frozenset[uuid.UUID]]:
    """Every (kind, counterpart) a hint can name, to its accounts: an owner's account
    slug for a ``manual_`` kind, an issuer key for a parser's pattern."""
    resolved: dict[tuple[str, str], set[uuid.UUID]] = defaultdict(set)
    for slug, account_id in uow.execute(select(account.c.slug, account.c.id)):
        resolved[(MANUAL_PREFIX, slug)].add(account_id)
    issued = (
        select(document.c.issuer, document_line.c.account_id)
        .distinct()
        .join(document, document.c.id == document_line.c.document_id)
        .where(document_line.c.account_id.is_not(None))
    )
    for issuer, account_id in uow.execute(issued):
        resolved[(PATTERN, issuer)].add(account_id)
    return {key: frozenset(ids) for key, ids in resolved.items()}


def _accounts(
    hint: tuple[str, str] | None,
    resolved: Mapping[tuple[str, str], frozenset[uuid.UUID]],
) -> frozenset[uuid.UUID]:
    """The accounts a hint names; empty when there is no hint or it names none."""
    if hint is None:
        return frozenset()
    kind, counterpart = hint
    key = MANUAL_PREFIX if kind.startswith(MANUAL_PREFIX) else PATTERN
    return resolved.get((key, counterpart), frozenset())


def within(a: _Line, b: _Line, tolerance: Tolerance) -> bool:
    """Whether the two legs net to zero within the tolerance (module docstring)."""
    pct = tolerance.pct / HUNDRED
    if a.currency == b.currency:
        net = a.amount + b.amount
        larger = a if abs(a.amount) >= abs(b.amount) else b
        if larger.cad is None or larger.cad == 0:
            allowed = abs(larger.amount) * pct
        else:
            cad = abs(larger.cad)
            allowed = max(tolerance.abs_cad, cad * pct) * abs(larger.amount) / cad
        return abs(net) <= allowed
    if a.cad is None or b.cad is None:
        return False
    larger_cad = max(abs(a.cad), abs(b.cad))
    return abs(a.cad + b.cad) <= max(tolerance.abs_cad, larger_cad * pct)


@dataclass(frozen=True)
class _Edge:
    debit: _Line
    credit: _Line


def _candidate_edges(
    lines: list[_Line],
    named: Mapping[uuid.UUID, frozenset[uuid.UUID]],
    tolerance: Tolerance,
    converting: frozenset[uuid.UUID],
) -> set[_Edge]:
    """Every ``own_transfer`` candidate (module docstring); ``converting`` holds the
    lines with a ``conversion`` candidate edge, which are neither end."""
    lines = [line for line in lines if line.id not in converting]
    by_day = sorted(lines, key=lambda line: (line.day, line.id))
    days = [line.day for line in by_day]
    window = dt.timedelta(days=tolerance.window_days)

    def fits(start: _Line, other: _Line) -> bool:
        return (
            other.account_id != start.account_id
            and (other.amount > 0) != (start.amount > 0)
            and not other.fee
            and (not other.card or other.transfer_like)
            and within(start, other, tolerance)
        )

    edges: set[_Edge] = set()
    for start in lines:
        if not start.transfer_like:
            continue
        first = bisect.bisect_left(days, start.day - window)
        last = bisect.bisect_right(days, start.day + window)
        found = [other for other in by_day[first:last] if fits(start, other)]
        hinted = [other for other in found if other.account_id in named[start.id]]
        for other in hinted or found:
            debit, credit = (start, other) if start.amount < 0 else (other, start)
            edges.add(_Edge(debit, credit))
    return edges


def _thinned(group: list[_Edge]) -> list[_Edge]:
    """The equal-card-payments tie-break (module docstring), or the group as it is."""
    debits = sorted({e.debit for e in group}, key=lambda line: line.day)
    credits = sorted({e.credit for e in group}, key=lambda line: line.day)
    amounts = {(line.currency, abs(line.amount)) for line in debits + credits}
    if (
        len(debits) < 2
        or len(debits) != len(credits)
        or len(group) != len(debits) * len(credits)
        or len(amounts) != 1
        or any(line.card for line in debits)
        or not all(line.card for line in credits)
        or len({line.day for line in debits}) != len(debits)
        or len({line.day for line in credits}) != len(credits)
    ):
        return group
    return [_Edge(d, c) for d, c in zip(debits, credits, strict=True)]


@dataclass(frozen=True)
class _Conversion:
    out: _Line
    into: _Line
    spread: Decimal | None
    printed_fee: uuid.UUID | None


def _in_cad(uow: UnitOfWork, books: dict[str, RateBook], leg: _Line, day: dt.date
            ) -> Decimal | None:  # fmt: skip
    """The in-leg's CAD value at the stored rate of the conversion date ``day``."""
    if leg.currency == QUOTE_CAD:
        return leg.amount
    if leg.currency not in books:
        books[leg.currency] = rate_book(uow, leg.currency)
    found = books[leg.currency].on_or_before(day)
    return None if found is None else cents(leg.amount * found[1])


def _printed_fee(out: _Line, into: _Line, fees: Sequence[_Line]) -> uuid.UUID | None:
    """The issuer's printed fee of a conversion: a fee line on its account, posted on
    a leg's date, whose wording is a leg's wording plus `` (fee)``."""
    wordings = {w + FEE_SUFFIX for w in out.wordings | into.wordings}
    found = [
        fee
        for fee in fees
        if fee.account_id == out.account_id
        and fee.day in (out.day, into.day)
        and fee.wordings & wordings
    ]
    return min(found, key=lambda fee: (fee.day, fee.id)).id if found else None


def _conversions(
    uow: UnitOfWork, lines: list[_Line], spread: Spread
) -> list[_Conversion]:
    """Every conversion candidate edge (module docstring)."""
    legs: dict[uuid.UUID, list[_Line]] = defaultdict(list)
    fees: list[_Line] = []
    for line in lines:
        if line.conversion:
            legs[line.account_id].append(line)
        elif line.fee:
            fees.append(line)
    books: dict[str, RateBook] = {}
    window = dt.timedelta(days=spread.window_days)
    found: list[_Conversion] = []
    for on_account in legs.values():
        for out in on_account:
            if out.amount > 0:
                continue
            for into in on_account:
                if (
                    into.amount < 0
                    or into.currency == out.currency
                    or abs(into.day - out.day) > window
                ):
                    continue
                into_cad = _in_cad(uow, books, into, out.day)
                residual: Decimal | None = None
                if out.cad is not None and into_cad is not None:
                    residual = -out.cad - into_cad
                    if abs(residual) > -out.cad * spread.pct / HUNDRED:
                        continue
                found.append(
                    _Conversion(out, into, residual, _printed_fee(out, into, fees))
                )
    return found


def pair_transfers(
    uow: UnitOfWork,
    *,
    tolerance: Tolerance,
    spread: Spread,
    hint_rows: Sequence[TransferHintRow],
) -> None:
    """Store every fiat ``own_transfer`` and ``conversion`` candidate edge and set
    ``transfer_like`` and ``hint_account_id`` (module docstring)."""
    lines = _lines(uow, hint_rows)
    resolved = _resolver(uow)
    named = {line.id: _accounts(line.hint, resolved) for line in lines}
    conversions = sorted(
        _conversions(uow, lines, spread),
        key=lambda c: (c.out.day, c.out.id, c.into.id),
    )
    converting = frozenset(leg.id for c in conversions for leg in (c.out, c.into))
    edges = [
        edge
        for group in pairing.groups(
            _candidate_edges(lines, named, tolerance, converting),
            lambda e: (e.debit.id, e.credit.id),
        )
        for edge in _thinned(group)
    ]
    edges.sort(key=lambda edge: (edge.debit.day, edge.debit.id, edge.credit.id))
    if edges:
        uow.execute(
            insert(pair_edge),
            [
                {
                    "id": derived_id(
                        edge.debit.day,
                        ("pair_edge", KIND, edge.debit.id, edge.credit.id),
                    ),
                    "kind": KIND,
                    "a_txn_id": edge.debit.id,
                    "b_txn_id": edge.credit.id,
                    "applied": False,
                }
                for edge in edges
            ],
        )
    if conversions:
        uow.execute(
            insert(pair_edge),
            [
                {
                    "id": derived_id(
                        c.out.day, ("pair_edge", CONVERSION, c.out.id, c.into.id)
                    ),
                    "kind": CONVERSION,
                    "a_txn_id": c.out.id,
                    "b_txn_id": c.into.id,
                    "applied": False,
                    "spread_cad": c.spread,
                    "fee_txn_id": c.printed_fee,
                }
                for c in conversions
            ],
        )
    marked = [
        {
            "b_id": line.id,
            "b_hint": next(iter(named[line.id])) if len(named[line.id]) == 1 else None,
        }
        for line in lines
        if line.transfer_like
    ]
    if marked:
        uow.execute(
            update(txn)
            .where(txn.c.id == bindparam("b_id"))
            .values(transfer_like=True, hint_account_id=bindparam("b_hint")),
            marked,
        )
