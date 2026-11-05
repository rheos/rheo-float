"""Stage 17: one item per excluded subject, including unapplied payment credits.

Orphans have one financial row per decision, however many replacements are offered.
Their money and date come from the stored snapshot, never from a guessed candidate.
A snapshotless orphan uses effective_on, else the stored UTC decision date, and has
no invented account or money. The report computes candidate counts independently.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from decimal import Decimal

from sqlalchemy import insert, select

from rheo_finance.context import UnitOfWork
from rheo_finance.decision import INVOICE_DECISIONS, effective, invoice_ref
from rheo_finance.derive.stage4 import cents
from rheo_finance.manual.document_periods import DocumentPeriodRow
from rheo_finance.manual.stage2 import account_id
from rheo_finance.storage.ids import derived_id
from rheo_finance.storage.refs import format_ref
from rheo_finance.storage.schema import (
    account,
    coverage,
    decision,
    decision_orphan,
    decision_target,
    document,
    document_line,
    open_item,
    payment,
    payment_application,
    payment_evidence,
    rejected_period,
    statement_period,
    txn,
    txn_source,
)


@dataclass(frozen=True, kw_only=True)
class Item:
    kind: str
    subject_ref: str
    occurred_on: dt.date
    state: str
    reason_code: str
    txn_id: uuid.UUID | None = None
    document_id: uuid.UUID | None = None
    decision_id: uuid.UUID | None = None
    payment_id: uuid.UUID | None = None
    account_id: uuid.UUID | None = None
    amount: Decimal | None = None
    currency: str | None = None
    cad_amount: Decimal | None = None
    reason_account_id: uuid.UUID | None = None
    reason_date: dt.date | None = None
    reason_payee_id: uuid.UUID | None = None
    category: str | None = None
    tag: str | None = None

    def stored(self) -> dict[str, object]:
        direction = None
        if self.amount is not None:
            direction = "out" if self.amount < 0 else "in"
        return asdict(self) | {
            "id": derived_id(
                self.occurred_on, ("open_item", self.kind, self.subject_ref)
            ),
            "tax_year": self.occurred_on.year,
            "direction": direction,
        }


def _document_accounts(
    uow: UnitOfWork, known_periods: Sequence[DocumentPeriodRow]
) -> set[uuid.UUID]:
    found: set[uuid.UUID] = set()
    for source in (document_line, statement_period, rejected_period):
        found.update(
            uow.scalars(
                select(source.c.account_id)
                .join(document, document.c.id == source.c.document_id)
                .where(source.c.account_id.is_not(None))
            )
        )
    paths = set(uow.scalars(select(document.c.path)))
    found.update(
        account_id(row.account) for row in known_periods if row.archive_path in paths
    )
    return found


def _lines(uow: UnitOfWork, known_periods: Sequence[DocumentPeriodRow]) -> list[Item]:
    accounts = {row.id: row.first_known_on for row in uow.execute(select(account))}
    has_document = _document_accounts(uow, known_periods)
    months = {
        (row.account_id, row.month): row.state for row in uow.execute(select(coverage))
    }
    usable: set[uuid.UUID] = set()
    for row in uow.execute(
        select(
            txn_source.c.txn_id, txn_source.c.manual_ref, document.c.status
        ).select_from(
            txn_source.outerjoin(
                document_line, txn_source.c.document_line_id == document_line.c.id
            ).outerjoin(document, document_line.c.document_id == document.c.id)
        )
    ):
        if (row.manual_ref and row.manual_ref.strip()) or (
            row.status is not None and row.status != "rejected"
        ):
            usable.add(row.txn_id)
    made: list[Item] = []
    for row in uow.execute(select(txn)):
        day = row.recognition_date or row.posting_date
        common = dict(
            subject_ref=format_ref("finance", "txn", row.id),
            txn_id=row.id,
            occurred_on=day,
            account_id=row.account_id,
            amount=row.amount,
            currency=row.currency,
            cad_amount=row.cad_amount,
        )
        if row.transfer_like and row.transfer_state in {"ambiguous", "unresolved"}:
            state = row.transfer_state
            reason, first = "no_match", None
            hint = row.hint_account_id
            if state == "ambiguous":
                reason = "candidates"
            elif hint is not None:
                if hint not in has_document:
                    reason = "counterpart_no_data"
                elif months.get((hint, row.posting_date.replace(day=1))) != "have":
                    reason = "counterpart_not_loaded"
                    known = accounts.get(hint)
                    if known is not None and row.posting_date < known:
                        first = known
            made.append(
                Item(
                    **common,
                    kind=f"{state}_transfer",
                    state=state,
                    reason_code=reason,
                    reason_account_id=hint,
                    reason_date=first,
                )
            )
        if row.conversion_state == "unconverted":
            made.append(
                Item(
                    **common,
                    kind="unconverted",
                    state="unconverted",
                    reason_code="no_rate",
                )
            )
        if row.id not in usable:
            made.append(
                Item(
                    **common,
                    kind="needs_source",
                    state="needs_source",
                    reason_code="no_source",
                )
            )
    return made


def _invoices(uow: UnitOfWork) -> list[Item]:
    linked = set(uow.scalars(select(txn_source.c.line_key_id)))
    seen: set[uuid.UUID] = set()
    made: list[Item] = []
    rows = uow.execute(
        select(document_line, document.c.sha256)
        .join(document, document.c.id == document_line.c.document_id)
        .where(
            document.c.status == "parsed",
            document_line.c.line_kind == "invoice",
            document_line.c.line_key_id.is_not(None),
        )
        .order_by(document.c.sha256.desc(), document_line.c.line_no)
    )
    for row in rows:
        key = row.line_key_id
        if key in linked or key in seen:
            continue
        seen.add(key)
        decided = effective(uow, INVOICE_DECISIONS, invoice_ref(key))
        # Stage 3 refuses an unsupported-currency decision and leaves it open.
        if decided is not None and row.refused_decision_id is None:
            continue
        if row.posting_date is None or row.amount is None or row.currency is None:
            continue
        made.append(
            Item(
                kind="unmatched_invoice",
                subject_ref=invoice_ref(key),
                document_id=row.document_id,
                occurred_on=row.posting_date,
                amount=row.amount,
                currency=row.currency,
                cad_amount=cents(row.amount) if row.currency == "CAD" else None,
                state="unmatched",
                reason_code="unmatched",
            )
        )
    return made


def _orphans(uow: UnitOfWork) -> list[Item]:
    candidates: dict[uuid.UUID, set[uuid.UUID]] = defaultdict(set)
    for row in uow.execute(select(decision_orphan)):
        offered = candidates[row.decision_id]
        if row.candidate_txn_id is not None:
            offered.add(row.candidate_txn_id)
    if not candidates:
        return []
    snapshots = defaultdict(list)
    for row in uow.execute(
        select(decision_target)
        .where(decision_target.c.decision_id.in_(candidates))
        .order_by(decision_target.c.seq)
    ):
        snapshots[row.decision_id].append(row)
    accounts = {row.slug: row.id for row in uow.execute(select(account))}
    made: list[Item] = []
    for row in uow.execute(select(decision).where(decision.c.id.in_(candidates))):
        offered = candidates[row.id]
        snapshot = next(iter(snapshots[row.id]), None)
        day = (
            snapshot.posting_date
            if snapshot is not None
            else row.effective_on or row.decided_at.astimezone(dt.UTC).date()
        )
        amount = None if snapshot is None else snapshot.amount
        currency = None if snapshot is None else snapshot.currency
        made.append(
            Item(
                kind="orphaned_decision",
                subject_ref=format_ref("finance", "decision", row.id),
                decision_id=row.id,
                txn_id=next(iter(offered)) if len(offered) == 1 else None,
                occurred_on=day,
                account_id=None
                if snapshot is None
                else accounts.get(snapshot.account_slug),
                amount=amount,
                currency=currency,
                cad_amount=cents(amount)
                if amount is not None and currency == "CAD"
                else None,
                category=row.category,
                tag=row.tag,
                state="orphaned",
                reason_code="orphan_candidate" if offered else "orphan_no_candidate",
            )
        )
    return made


def _credits(uow: UnitOfWork) -> list[Item]:
    money = {
        row.payment_id: row
        for row in uow.execute(
            select(payment_evidence.c.payment_id, txn)
            .join(txn, txn.c.id == payment_evidence.c.txn_id)
            .where(payment_evidence.c.role == "money")
        )
    }
    made: list[Item] = []
    for row in uow.execute(
        select(
            payment_application.c.id,
            payment_application.c.amount,
            payment_application.c.reason,
            payment_application.c.bound_on,
            payment.c.id.label("payment_id"),
            payment.c.paid_on,
            payment.c.currency,
            payment.c.payee_id,
        )
        .join(payment, payment.c.id == payment_application.c.payment_id)
        .where(payment_application.c.obligation_id.is_(None))
    ):
        line = money.get(row.payment_id)
        signed = -row.amount if line is None or line.amount < 0 else row.amount
        cad = cents(signed) if row.currency == "CAD" else None
        if line is not None and line.cad_amount is not None:
            cad = cents(signed * abs(line.cad_amount / line.amount))
        made.append(
            Item(
                kind="unapplied_credit",
                subject_ref=format_ref("finance", "payment_application", row.id),
                payment_id=row.payment_id,
                txn_id=None if line is None else line.id,
                occurred_on=row.paid_on,
                account_id=None if line is None else line.account_id,
                amount=signed,
                currency=row.currency,
                cad_amount=cad,
                state="unapplied",
                reason_code="excess" if row.reason == "excess" else "no_target",
                reason_payee_id=row.payee_id,
                reason_date=row.bound_on,
            )
        )
    return made


def build(uow: UnitOfWork, *, known_periods: Sequence[DocumentPeriodRow] = ()) -> int:
    """Fill after orphans and payment application; the caller cleared open_item."""
    made = [
        *_lines(uow, known_periods),
        *_invoices(uow),
        *_orphans(uow),
        *_credits(uow),
    ]
    if made:
        uow.execute(insert(open_item), [each.stored() for each in made])
    return len(made)
