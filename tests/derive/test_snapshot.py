"""The real snapshot provider over ``txn``: a decision recorded on a line reference gets
its ``decision_target`` row, and the reference survives a provisional line's
replacement (Key decision 19, edge case 9). Every value is synthetic.
"""

from __future__ import annotations

import dataclasses
import uuid
from decimal import Decimal

from sqlalchemy import insert, select

from rheo_finance import catalogue
from rheo_finance.context import LineSnapshot
from rheo_finance.derive.snapshot import txn_snapshot
from rheo_finance.operations import dispatch
from rheo_finance.storage.ids import derived_id
from rheo_finance.storage.refs import format_ref
from rheo_finance.storage.schema import decision_target, txn_source

from .support import FIXTURES, D, Ledger

CSV_FIXTURE = (FIXTURES / "vancity_chequing_jump.csv").read_text(encoding="utf-8")


def ref_of(txn_id: object) -> str:
    assert isinstance(txn_id, uuid.UUID)
    return format_ref("finance", "txn", txn_id)


def grocer_ref(ledger: Ledger) -> str:
    row = next(r for r in ledger.txns() if r["posting_date"] == D(2026, 2, 16))
    return ref_of(row["id"])


def test_a_line_reference_resolves_to_its_snapshot(ledger: Ledger) -> None:
    ledger.chequing()
    ledger.file("vancity/export.csv", CSV_FIXTURE)
    ledger.derive()

    assert txn_snapshot(ledger.uow, grocer_ref(ledger)) == LineSnapshot(
        account_slug="chequing",
        amount=Decimal("-9.99"),
        currency="CAD",
        posting_date=D(2026, 2, 16),
    )


def test_a_reference_no_line_carries_resolves_to_nothing(ledger: Ledger) -> None:
    ledger.chequing()
    ledger.file("vancity/export.csv", CSV_FIXTURE)
    ledger.derive()
    unknown = ref_of(derived_id(D(2026, 2, 16), ("fiat", "nobody")))
    document = format_ref("finance", "document", derived_id(D(2026, 2, 16), ("d",)))

    assert txn_snapshot(ledger.uow, unknown) is None
    assert txn_snapshot(ledger.uow, document) is None  # not a line type
    assert txn_snapshot(ledger.uow, "not a reference") is None


def test_a_decision_on_a_line_reference_gets_its_snapshot_row(ledger: Ledger) -> None:
    ledger.chequing()
    ledger.file("vancity/export.csv", CSV_FIXTURE)
    ledger.derive()
    catalogue.load_all()
    ref = grocer_ref(ledger)
    ctx = dataclasses.replace(ledger.ctx, snapshot_provider=txn_snapshot)

    out = dispatch(
        "finance.decision.record",
        {"kind": "category", "target_ref": ref, "category": "groceries"},
        ctx.role,
        ctx=ctx,
    )

    target = ledger.conn.execute(
        select(decision_target).where(decision_target.c.decision_id == out.decision_id)  # type: ignore[attr-defined]
    ).one()
    assert (target.target_ref, target.account_slug, target.amount) == (
        ref,
        "chequing",
        Decimal("-9.99"),
    )
    assert (target.currency, target.posting_date) == ("CAD", D(2026, 2, 16))


def test_the_reference_of_a_replaced_line_still_resolves(ledger: Ledger) -> None:
    # A provisional line's key stays on its txn as an additional source when a
    # statement line becomes the primary: the old reference names the same txn. The
    # additional source here is a manual reference, as a later stage may write one.
    ledger.chequing()
    ledger.file("vancity/export.csv", CSV_FIXTURE)
    ledger.derive()
    row = next(r for r in ledger.txns() if r["posting_date"] == D(2026, 2, 16))
    earlier = derived_id(D(2026, 2, 15), ("fiat", "earlier key"))
    ledger.conn.execute(
        insert(txn_source).values(
            txn_id=row["id"], manual_ref="manual:transactions.csv:9",
            line_key_id=earlier, role="additional",
        )
    )  # fmt: skip

    snapshot = txn_snapshot(ledger.uow, ref_of(earlier))

    assert snapshot is not None
    assert (snapshot.amount, snapshot.posting_date) == (
        Decimal("-9.99"),
        D(2026, 2, 16),
    )


def test_an_evidence_source_is_not_a_line_of_the_payment(ledger: Ledger) -> None:
    ledger.chequing()
    ledger.file("vancity/export.csv", CSV_FIXTURE)
    ledger.derive()
    row = next(r for r in ledger.txns() if r["posting_date"] == D(2026, 2, 16))
    receipt = derived_id(D(2026, 2, 15), ("fiat", "receipt key"))
    ledger.conn.execute(
        insert(txn_source).values(
            txn_id=row["id"], manual_ref="manual:transactions.csv:10",
            line_key_id=receipt, role="evidence",
        )
    )  # fmt: skip

    assert txn_snapshot(ledger.uow, ref_of(receipt)) is None
