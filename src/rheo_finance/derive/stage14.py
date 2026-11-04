"""Derivation stage 14: the obligations (FR-19, FR-21; Key decision 20; SPEC Data Models
``obligation``). Nothing outside this stage creates one, and a card charge from a
subscription vendor is a spending line only. The paths are in the table above
``create_obligations``.

- (1) A card statement: a parsed statement that states a balance owed gives
  ``card_statement``, period ``card-YYYY-MM-DD`` of its closing date, issued on it.
  ``threshold_due`` is the printed minimum, else the full balance.
- (2) A parsed bill: a parsed document of kind ``bill`` gives ``bill``, period
  ``bill-YYYY-MM`` of its due month, issued on its printed bill date. A later bill of
  one payee due in one month takes ``-2``, ``-3`` in (bill date, due date, path) order,
  which ``report_status`` counts for status line 9. A parsed bill that prints no total
  due has no amount to owe and makes no obligation.
- (3) A manual bill: one ``bill`` per ``obligations.csv`` row, typed in whether or not a
  parser reads that issuer's bills. The document its row names, when one is registered
  at that path, is its primary source and makes nothing of its own; otherwise the row
  is.
- (4) An arrangement: one ``arrangement_instalment`` per ``arrangements.csv`` row,
  period ``inst-<slug>-<n>``, issued on ``agreed_on``; and arrears, below.
- (5) A manual debt: one ``manual_debt`` per ``obligations.csv`` row, period
  ``debt-<slug>``, with the row's ``issued_on`` and due date, either of which may be
  null; and one per invoice stage 3 found unpaid (``evidence.UnpaidInvoice``: no
  payment matches it and the decision in force is ``invoice_unpaid``). That one's period
  is ``debt-inv-<line key>``, the key of the invoice line, which every re-saved copy of
  the invoice document shares, so a new copy keeps the id a decision names. It is owed
  the smallest amount due its copies print, else the total, issued on the invoice's date
  and due on the decision's date if it gives one; ``invoice_ref`` names the document
  that counts. Its
  payee is the first payee whose patterns name the printed payee, else the vendor payee
  of the invoice's archive directory (``manual.payees``).

A document ``precedence.supersede`` marked as a copy is not parsed, so it makes nothing.
Two different parsed statements of one card with one closing date have one natural key,
so they are one obligation: the one ``precedence`` ranks first. When their balance,
minimum or due date differ, the row's ``statement_conflict`` says so, for status line 9.

**A manual row and a document.** A manual row with the key of a parsed document is one
obligation with it: the document's figures win, the row stays in ``manual_source``, and
the row's amount less the document's printed total is ``manual_mismatch``, null when
they agree. A ``bill`` row's minimum is kept as written and is never its threshold: only
a card's threshold is its minimum.

**The payee.** The payee standing for the account (``payee.account_id``), else the
payee named by the account's slug for a statement or the issuer's slug for a bill. Stage
2 makes a card's and an issuer's exist (``manual.payees``). A statement that states an
amount owed on an account not registered as a card has none: ``NoPayee`` fails the
derive rather than dropping what is owed. A manual row and an arrangement name their
payee by slug, and the loaders refuse an unknown one.

**A bill row naming the issuer.** An account with its own payee takes its parsed bills
from the issuer's payee, so a typed bill naming the issuer's slug would be owed beside
the parsed bill it copies. When every parsed bill of the issuer is owed to one other
payee, the row is read as that payee's and merges or chains there. Parsed bills owed to
two or more other payees leave nothing to say which one it means: the row stays on the
issuer's payee and is ``unjoined``. With no parsed bill yet there is nothing to count
twice, and the row stays where it was written.

**Supersession (FR-21).** Per (payee, account, kind), so two accounts of one issuer stay
two chains, oldest issue first, each newer row names the one before it in
``supersedes_id`` and the one before it is ``superseded``. A statement's balance and
minimum already hold what the previous one carried, and a bill's total holds the unpaid
balance before it, so only the newest is owed.

**Which manual bills chain.** A payee is a bill source when it has a parsed bill, is a
bill issuer (``manual.payees.bill_issuers``), or stands for a ``bill_vendor`` account.
Its bills are one running account, typed in or parsed: a manual bill joins the chain by
``issued_on``, so a newer bill supersedes an older one in either direction and a bill
typed from a photo is never owed beside the bill that carries it. Its account is the
one the payee's parsed bills are on, else the one the payee stands for. Parsed bills on
two accounts leave it no account to join: it then chains only with the payee's other
manual bills and is ``unjoined``, which status line 9 counts, because it stays open
beside the parsed bills that may carry it. Any other payee is invoice-style: a manual
invoice does not carry the one before it, so two of them are both owed. A manual debt
never chains, and a manual row merged into a document is that document's row.

**Arrangements and arrears.** Every bill period an arrangement ``replaces`` is
``superseded``, and names the arrangement in ``arrangement_ref``, from then on: when the
last instalment is paid the arrangement stops being active, and the replaced periods
must not come back as owed. An arrangement is active until stage 15's application has
paid every one of its instalments (``finished``, handed back by ``derive.run``). While
an arrangement of a payee is active, the newest bill
of each of its accounts is owed for its printed current charges only, or for its total
less its printed arrears when it prints no current charges. A typed-in bill prints
neither, so it is owed as typed. With none active, a bill that prints an arrears line
is split: the bill is owed its total less the arrears, and an ``arrears`` row, period
``arrears-YYYY-MM`` of the bill date, holds the arrears, so the two add up to the total
printed and instalments and arrears are never both owed. Only the newest bill of an
account states arrears: an older figure is folded into it. A carried balance that is
not labelled arrears (a bill's unpaid balance) stays in the bill's own total, which one
payment can then match.

The total printed caps all of this. A credit carried on a bill can leave its total below
its current charges or its arrears line, and nothing is owed above what the bill asks
for: neither figure raises what is owed past the total, and neither takes a bill with a
total of zero or more below zero.

Rows leave here with ``paid`` zero, and a row with nothing remaining (a card at zero or
in credit) is ``paid`` rather than open; stage 15's application then writes what was
paid.

The id comes from the natural key alone, on the fixed registry date, so a figure or a
date that changes keeps the id a decision names.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections import Counter, defaultdict
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from decimal import Decimal
from itertools import pairwise
from typing import Final, Literal

from sqlalchemy import Row, insert, select

from rheo_finance.context import UnitOfWork
from rheo_finance.derive import precedence
from rheo_finance.derive.evidence import UnpaidInvoice
from rheo_finance.derive.stage4 import cents
from rheo_finance.manual.accounts import BILL_VENDOR
from rheo_finance.manual.arrangements import Arrangement
from rheo_finance.manual.obligations import ManualObligation, bill_period
from rheo_finance.manual.payees import Payees, bill_issuers
from rheo_finance.manual.stage2 import REGISTRY_DATE
from rheo_finance.storage import refs
from rheo_finance.storage.ids import derived_id
from rheo_finance.storage.schema import (
    account,
    document,
    obligation,
    payee,
    statement_period,
)

ObligationKind = Literal[
    "card_statement", "bill", "arrangement_instalment", "arrears", "manual_debt"
]

CARD_STATEMENT: Final[ObligationKind] = "card_statement"
BILL: Final[ObligationKind] = "bill"
INSTALMENT: Final[ObligationKind] = "arrangement_instalment"
ARREARS: Final[ObligationKind] = "arrears"
MANUAL_DEBT: Final[ObligationKind] = "manual_debt"
DOCUMENT_REF_TYPE: Final = "document"
CURRENCY: Final = "CAD"
ZERO: Final = Decimal("0.00")

_PayeeOf = Callable[[Row[tuple[object, ...]], str], uuid.UUID]


class NoPayee(LookupError):
    """A parsed document states an amount owed and nothing names who it is owed to."""


def obligation_id(
    payee_id: uuid.UUID, kind: ObligationKind, period_label: str
) -> uuid.UUID:
    """The id of the obligation with this natural key."""
    return derived_id(REGISTRY_DATE, ("obligation", payee_id, kind, period_label))


@dataclass(frozen=True)
class _Draft:
    """One obligation before its place in its chain is known. ``account_id`` is the
    account its chain runs on; the current charges and the arrears are a parsed
    document's; ``chained`` says it takes part in supersession."""

    payee_id: uuid.UUID
    kind: ObligationKind
    period_label: str
    statement_date: dt.date | None
    issued_on: dt.date | None
    amount_due: Decimal
    minimum_due: Decimal | None
    due_date: dt.date | None
    source: str
    account_id: uuid.UUID | None = None
    chained: bool = False
    current_charges: Decimal | None = None
    arrears: Decimal | None = None
    manual_source: str | None = None
    manual_mismatch: Decimal | None = None
    statement_conflict: bool = False
    arrangement_ref: str | None = None
    invoice_ref: str | None = None
    unjoined: bool = False

    @property
    def id(self) -> uuid.UUID:
        return obligation_id(self.payee_id, self.kind, self.period_label)

    @property
    def replaced(self) -> bool:
        """An arrangement names this bill's period as replaced."""
        return self.kind == BILL and self.arrangement_ref is not None


def _periods(uow: UnitOfWork) -> list[Row[tuple[object, ...]]]:
    """The parsed periods that state an amount owed, with their document and account.
    Only ``CURRENCY``: every card and bill parser prints it, and ``amount_due`` is
    CAD."""
    sp = statement_period.c
    return list(
        uow.execute(
            select(
                sp.account_id,
                sp.period_end,
                sp.balance_due,
                sp.minimum_due,
                sp.due_date,
                sp.bill_date,
                sp.current_charges,
                sp.arrears,
                document.c.id.label("document_id"),
                document.c.path,
                document.c.sha256,
                document.c.kind,
                document.c.issuer,
                account.c.slug.label("account_slug"),
            )
            .select_from(
                statement_period.join(document, sp.document_id == document.c.id).join(
                    account, sp.account_id == account.c.id
                )
            )
            .where(
                document.c.status == "parsed",
                sp.currency == CURRENCY,
                sp.balance_due.is_not(None),
            )
            .order_by(sp.period_end, document.c.path, document.c.sha256)
        ).all()
    )


def _payees(
    uow: UnitOfWork,
) -> tuple[Mapping[uuid.UUID, uuid.UUID], Mapping[str, uuid.UUID]]:
    """Payee ids by the account each stands for, and by slug."""
    rows = uow.execute(select(payee.c.id, payee.c.slug, payee.c.account_id)).all()
    return (
        {row.account_id: row.id for row in rows if row.account_id is not None},
        {row.slug: row.id for row in rows},
    )


def _optional_cents(value: Decimal | None) -> Decimal | None:
    return None if value is None else cents(value)


def _card_drafts(
    uow: UnitOfWork, rows: Iterable[Row[tuple[object, ...]]], payee_of: _PayeeOf
) -> list[_Draft]:
    """One draft per natural key, oldest closing date first: the document ``precedence``
    ranks first holds it. A payee stands for one account, so a key's documents are all
    on one account."""
    groups: dict[tuple[uuid.UUID, dt.date], list[Row[tuple[object, ...]]]] = (
        defaultdict(list)
    )
    for row in rows:
        groups[(payee_of(row, row.account_slug), row.period_end)].append(row)
    ranks = (
        precedence.standings(uow)
        if any(len(group) > 1 for group in groups.values())
        else {}
    )
    drafts = []
    for (payee_id, closing), group in groups.items():
        row = group[0]
        if len(group) > 1:
            place = {
                each.document_id: n for n, each in enumerate(ranks[row.account_id])
            }
            row = min(group, key=lambda each: place[each.document_id])
        figures = {
            (each.balance_due, each.minimum_due, each.due_date) for each in group
        }
        drafts.append(
            _Draft(
                payee_id=payee_id,
                kind=CARD_STATEMENT,
                period_label=f"card-{closing:%Y-%m-%d}",
                statement_date=closing,
                issued_on=closing,
                amount_due=cents(row.balance_due),
                minimum_due=_optional_cents(row.minimum_due),
                due_date=row.due_date,
                source=row.path,
                account_id=row.account_id,
                chained=True,
                statement_conflict=len(figures) > 1,
            )
        )
    return drafts


def _bill_drafts(
    rows: Iterable[Row[tuple[object, ...]]], payee_of: _PayeeOf
) -> list[_Draft]:
    """A draft per bill, in the order that numbers a due month's second bill."""
    ordered = sorted(
        rows, key=lambda row: (row.bill_date, row.due_date, row.path, row.sha256)
    )
    seen: Counter[tuple[uuid.UUID, str]] = Counter()
    drafts = []
    for row in ordered:
        payee_id = payee_of(row, row.issuer)
        month = bill_period(row.due_date)
        seen[(payee_id, month)] += 1
        place = seen[(payee_id, month)]
        drafts.append(
            _Draft(
                payee_id=payee_id,
                kind=BILL,
                period_label=month if place == 1 else f"{month}-{place}",
                statement_date=None,
                issued_on=row.bill_date,
                amount_due=cents(row.balance_due),
                minimum_due=None,
                due_date=row.due_date,
                source=row.path,
                account_id=row.account_id,
                chained=True,
                current_charges=_optional_cents(row.current_charges),
                arrears=_optional_cents(row.arrears),
            )
        )
    return drafts


def _issuer_holders(
    rows: Iterable[Row[tuple[object, ...]]],
    payee_of: _PayeeOf,
    by_slug: Mapping[str, uuid.UUID],
) -> Mapping[uuid.UUID, set[uuid.UUID]]:
    """Per issuer's slug payee, the payees that issuer's parsed bills are owed to."""
    holders: dict[uuid.UUID, set[uuid.UUID]] = defaultdict(set)
    for row in rows:
        named = by_slug.get(row.issuer)
        if named is not None:
            holders[named].add(payee_of(row, row.issuer))
    return holders


def _manual_drafts(
    uow: UnitOfWork,
    manual: Sequence[ManualObligation],
    by_slug: Mapping[str, uuid.UUID],
    holders: Mapping[uuid.UUID, set[uuid.UUID]],
) -> list[_Draft]:
    """A draft per ``obligations.csv`` row, a bill naming an issuer whose parsed bills
    all go to one other payee read as that payee's. Each row's source is the document it
    names, if one is registered at that path, else the row itself."""
    named = {each.row.archive_path for each in manual if each.row.archive_path}
    registered = set(
        uow.execute(select(document.c.path).where(document.c.path.in_(named))).scalars()
    )

    def source_of(each: ManualObligation) -> str:
        path = each.row.archive_path
        return path if path is not None and path in registered else each.source_ref

    standing = {
        named: next(iter(found))
        for named, found in holders.items()
        if len(found) == 1 and named not in found
    }

    def payee_id(each: ManualObligation) -> uuid.UUID:
        named = by_slug[each.row.payee]
        return standing.get(named, named) if each.row.kind == BILL else named

    return [
        _Draft(
            payee_id=payee_id(each),
            kind=each.row.kind,
            period_label=each.row.period,
            statement_date=None,
            issued_on=each.row.issued_on,
            amount_due=each.row.amount_due,
            minimum_due=each.row.minimum,
            due_date=each.row.due_date,
            source=source_of(each),
            manual_source=each.source_ref,
        )
        for each in manual
    ]


def _invoice_debt_drafts(
    unpaid: Iterable[UnpaidInvoice], payees: Payees, by_slug: Mapping[str, uuid.UUID]
) -> list[_Draft]:
    """A debt per invoice stage 3 found unpaid."""
    drafts = []
    for each in unpaid:
        named = payees.naming(each.payee)
        slug = each.issuer.partition("/")[0] if named is None else named.slug
        drafts.append(
            _Draft(
                payee_id=by_slug[slug],
                kind=MANUAL_DEBT,
                period_label=f"debt-inv-{each.line_key}",
                statement_date=None,
                issued_on=each.invoice_date,
                amount_due=cents(each.owed),
                minimum_due=None,
                due_date=each.due_date,
                source=each.path,
                invoice_ref=refs.format_ref(
                    refs.FINANCE_SYSTEM, DOCUMENT_REF_TYPE, each.document_id
                ),
            )
        )
    return drafts


def _merged(documents: Iterable[_Draft], manual: Iterable[_Draft]) -> list[_Draft]:
    """``documents``, each holding the manual row of its key if there is one, then the
    manual rows that stand alone."""
    by_key = {draft.id: draft for draft in documents}
    alone = []
    for row in manual:
        found = by_key.get(row.id)
        if found is None:
            alone.append(row)
            continue
        by_key[row.id] = replace(
            found,
            manual_source=row.manual_source,
            manual_mismatch=(row.amount_due - found.amount_due) or None,
        )
    return [*by_key.values(), *alone]


def _bill_sources(
    uow: UnitOfWork, documents: Iterable[_Draft], by_slug: Mapping[str, uuid.UUID]
) -> Mapping[uuid.UUID, uuid.UUID | None]:
    """Every bill-source payee, and the account its manual bills chain on: the one its
    parsed bills are on, else the one it stands for, else none."""
    parsed: dict[uuid.UUID, set[uuid.UUID]] = defaultdict(set)
    for draft in documents:
        if draft.kind == BILL and draft.account_id is not None:
            parsed[draft.payee_id].add(draft.account_id)
    stands_for: dict[uuid.UUID, uuid.UUID] = {
        row.id: row.account_id
        for row in uow.execute(
            select(payee.c.id, payee.c.account_id)
            .select_from(payee.join(account, payee.c.account_id == account.c.id))
            .where(account.c.kind == BILL_VENDOR)
        )
    }
    sources: dict[uuid.UUID, uuid.UUID | None] = {
        by_slug[slug]: None for slug in bill_issuers()
    }
    sources.update(stands_for)
    for payee_id, accounts in parsed.items():
        sources[payee_id] = next(iter(accounts)) if len(accounts) == 1 else None
    return sources


def _with_manual_chained(
    drafts: Iterable[_Draft],
    sources: Mapping[uuid.UUID, uuid.UUID | None],
    billed: Collection[uuid.UUID],
) -> list[_Draft]:
    """``drafts``, each manual bill of a bill source in its chain, and ``unjoined`` when
    the payee is in ``billed`` (it has parsed bills) and no account takes the row."""
    return [
        replace(
            draft,
            chained=True,
            account_id=sources[draft.payee_id],
            unjoined=sources[draft.payee_id] is None and draft.payee_id in billed,
        )
        if draft.kind == BILL and not draft.chained and draft.payee_id in sources
        else draft
        for draft in drafts
    ]


def _in_issue_order(drafts: Iterable[_Draft]) -> list[_Draft]:
    """``drafts`` oldest issue first; ties stay in the order they came, documents before
    manual rows."""
    return sorted(
        drafts,
        key=lambda draft: (
            draft.issued_on or dt.date.min,
            draft.due_date or dt.date.min,
        ),
    )


def _instalment_drafts(
    arrangements: Iterable[Arrangement], by_slug: Mapping[str, uuid.UUID]
) -> list[_Draft]:
    return [
        _Draft(
            payee_id=by_slug[found.payee],
            kind=INSTALMENT,
            period_label=each.row.period,
            statement_date=None,
            issued_on=each.row.issued_on,
            amount_due=each.row.amount,
            minimum_due=None,
            due_date=each.row.due_date,
            source=each.source_ref,
            arrangement_ref=found.slug,
        )
        for found in arrangements
        for each in found.instalments
    ]


def _with_replaced(
    drafts: Iterable[_Draft],
    arrangements: Iterable[Arrangement],
    by_slug: Mapping[str, uuid.UUID],
) -> list[_Draft]:
    """``drafts``, each bill an arrangement replaces naming the first that does."""
    replacing: dict[uuid.UUID, str] = {}
    for found in arrangements:
        for period in found.replaces_periods:
            key = obligation_id(by_slug[found.payee], BILL, period)
            replacing.setdefault(key, found.slug)
    return [
        replace(draft, arrangement_ref=replacing[draft.id])
        if draft.id in replacing
        else draft
        for draft in drafts
    ]


def _active_payees(
    arrangements: Iterable[Arrangement],
    by_slug: Mapping[str, uuid.UUID],
    finished: Collection[str],
) -> set[uuid.UUID]:
    """The payees with an arrangement that has an instalment not yet paid: every
    arrangement but the ``finished`` ones (stage 15's application says which)."""
    return {
        by_slug[found.payee] for found in arrangements if found.slug not in finished
    }


def _owed_now(
    drafts: Sequence[_Draft], active: set[uuid.UUID]
) -> tuple[Mapping[uuid.UUID, _Draft], list[_Draft]]:
    """What the arrangement rule changes: the bills owed less than their total, by id,
    and the arrears rows. ``drafts`` comes oldest first, so the last chained bill of a
    (payee, account) is its newest; a replaced one is owed nothing either way."""
    newest = {
        (draft.payee_id, draft.account_id): draft
        for draft in drafts
        if draft.chained and draft.kind == BILL
    }
    changed: dict[uuid.UUID, _Draft] = {}
    arrears: list[_Draft] = []
    seen: Counter[tuple[uuid.UUID, str]] = Counter()
    for bill in newest.values():
        if bill.replaced:
            continue
        total = bill.amount_due
        printed = min(max(bill.arrears or ZERO, ZERO), max(total, ZERO))
        if bill.payee_id in active:
            owed = total - printed
            if bill.current_charges is not None:
                owed = max(min(bill.current_charges, total), min(total, ZERO))
            changed[bill.id] = replace(bill, amount_due=owed)
        elif printed:
            assert bill.issued_on is not None  # a bill that prints arrears is parsed
            month = f"arrears-{bill.issued_on:%Y-%m}"
            seen[(bill.payee_id, month)] += 1
            place = seen[(bill.payee_id, month)]
            changed[bill.id] = replace(bill, amount_due=total - printed)
            arrears.append(
                _Draft(
                    payee_id=bill.payee_id,
                    kind=ARREARS,
                    period_label=month if place == 1 else f"{month}-{place}",
                    statement_date=None,
                    issued_on=bill.issued_on,
                    amount_due=printed,
                    minimum_due=None,
                    due_date=bill.due_date,
                    source=bill.source,
                    account_id=bill.account_id,
                )
            )
    return changed, arrears


def _values(
    draft: _Draft, *, supersedes: _Draft | None, superseded: bool
) -> dict[str, object]:
    threshold = draft.amount_due
    if draft.kind == CARD_STATEMENT and draft.minimum_due is not None:
        threshold = draft.minimum_due
    remaining = max(draft.amount_due, ZERO)
    status = "superseded" if superseded else "open" if remaining else "paid"
    return {
        "id": draft.id,
        "payee_id": draft.payee_id,
        "kind": draft.kind,
        "period_label": draft.period_label,
        "statement_date": draft.statement_date,
        "issued_on": draft.issued_on,
        "amount_due": draft.amount_due,
        "minimum_due": draft.minimum_due,
        "threshold_due": threshold,
        "paid": ZERO,
        "remaining": remaining,
        "threshold_remaining": max(threshold, ZERO),
        "due_date": draft.due_date,
        "status": status,
        "primary_source": draft.source,
        "manual_source": draft.manual_source,
        "manual_mismatch": draft.manual_mismatch,
        "statement_conflict": draft.statement_conflict,
        "unjoined": draft.unjoined,
        "supersedes_id": None if supersedes is None else supersedes.id,
        "arrangement_ref": draft.arrangement_ref,
        "invoice_ref": draft.invoice_ref,
        "payments_mismatch": None,
    }


def _rows(drafts: Iterable[_Draft]) -> list[dict[str, object]]:
    """Every draft's row. Oldest to newest per (payee, account, kind), each newer
    chained one supersedes the one before it; ``drafts`` comes in that order."""
    chains: dict[tuple[uuid.UUID, uuid.UUID | None, ObligationKind], list[_Draft]] = (
        defaultdict(list)
    )
    rows: list[dict[str, object]] = []
    for draft in drafts:
        if draft.chained:
            chains[(draft.payee_id, draft.account_id, draft.kind)].append(draft)
        else:
            rows.append(_values(draft, supersedes=None, superseded=draft.replaced))
    for chain in chains.values():
        before: dict[uuid.UUID, _Draft] = {
            newer.id: older for older, newer in pairwise(chain)
        }
        newest = chain[-1].id
        rows.extend(
            _values(
                each,
                supersedes=before.get(each.id),
                superseded=each.replaced or each.id != newest,
            )
            for each in chain
        )
    return rows


# The only creators of an obligation (FR-19; AC-87). A sixth is a spec change.
#
#   path  source                            kind                    period label
#   (1)   a parsed statement                card_statement          card-YYYY-MM-DD
#   (2)   a parsed bill                     bill                    bill-YYYY-MM[-n]
#   (3)   an obligations.csv bill row       bill                    bill-YYYY-MM[-n]
#   (4)   an arrangements.csv row           arrangement_instalment  inst-<slug>-<n>
#   (4)   a parsed bill's arrears line      arrears                 arrears-YYYY-MM[-n]
#   (5)   an obligations.csv debt row       manual_debt             debt-<slug>
#   (5)   an invoice_unpaid decision        manual_debt             debt-inv-<line key>
def create_obligations(
    uow: UnitOfWork,
    manual: Sequence[ManualObligation],
    arrangements: Sequence[Arrangement],
    payees: Payees,
    unpaid_invoices: Iterable[UnpaidInvoice],
    finished: Collection[str] = frozenset(),
) -> int:
    """Rebuild ``obligation`` from the parsed statements and bills, stage 2's manual
    rows and arrangements, and the invoices stage 3 found unpaid; how many it made.
    ``finished`` names the arrangements whose instalments are all paid. The caller
    cleared the table."""
    by_account, by_slug = _payees(uow)

    def payee_of(row: Row[tuple[object, ...]], slug: str) -> uuid.UUID:
        found = by_account.get(row.account_id) or by_slug.get(slug)
        if found is None:
            raise NoPayee(
                f"account {row.account_slug}: a parsed {row.kind} states an amount"
                " owed and no payee has this slug; register the account as a"
                " credit_card, or add a payee of that slug"
            )
        return found

    periods = _periods(uow)
    cards = [row for row in periods if row.kind == "statement"]
    bills = [row for row in periods if row.kind == "bill"]
    documents = [
        *_card_drafts(uow, cards, payee_of),
        *_bill_drafts(bills, payee_of),
        *_invoice_debt_drafts(unpaid_invoices, payees, by_slug),
    ]
    holders = _issuer_holders(bills, payee_of, by_slug)
    merged = _merged(documents, _manual_drafts(uow, manual, by_slug, holders))
    chained = _with_manual_chained(
        merged, _bill_sources(uow, documents, by_slug), holders
    )
    drafts = _in_issue_order(_with_replaced(chained, arrangements, by_slug))
    changed, arrears = _owed_now(
        drafts, _active_payees(arrangements, by_slug, finished)
    )
    rows = _rows(
        [
            *(changed.get(draft.id, draft) for draft in drafts),
            *_instalment_drafts(arrangements, by_slug),
            *arrears,
        ]
    )
    if rows:
        uow.execute(insert(obligation), rows)
    return len(rows)
