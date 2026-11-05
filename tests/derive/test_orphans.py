"""Parser-version changes through derive.run and public decision operations.

All inputs are synthetic. No replacement inherits an answer before an explicit
reattachment, and every decline and reversal survives a fresh parse.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import uuid
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from rheo_finance import catalogue
from rheo_finance.clock import FakeClock
from rheo_finance.decision import RecordOut
from rheo_finance.derive.run import run
from rheo_finance.derive.snapshot import txn_snapshot
from rheo_finance.operations import dispatch
from rheo_finance.parsers import registry
from rheo_finance.parsers.base import AccountLookup, ParsedLine, ParseResult
from rheo_finance.settings import parse_settings
from rheo_finance.storage.refs import format_ref
from rheo_finance.storage.schema import (
    decision,
    decision_orphan,
    obligation,
    pair_edge,
    report_status,
    txn,
)

from .support import CARD_IDENTIFIER, CARD_PARSERS, Ledger, read_as_text


def setup(ledger: Ledger) -> None:
    ledger.card()
    ledger.file(
        "synthetic-card/lines.txt",
        "\n".join(
            (
                "SYNTHETIC CARD TRANSACTIONS",
                f"ACCOUNT {CARD_IDENTIFIER}",
                "LINE 2026-02-02 - -9.99 Sample purchase",
                "LINE 2026-02-03 - -20.00 Sample debit",
                "LINE 2026-02-03 - 20.00 Sample credit",
            )
        ),
    )
    ledger.derive()
    catalogue.load_all()
    ledger.ctx = dataclasses.replace(ledger.ctx, snapshot_provider=txn_snapshot)


def record(ledger: Ledger, **payload: object) -> uuid.UUID:
    out = dispatch("finance.decision.record", payload, ledger.ctx.role, ctx=ledger.ctx)
    assert isinstance(out, RecordOut)
    assert isinstance(ledger.ctx.clock, FakeClock)
    ledger.ctx.clock.advance(dt.timedelta(minutes=1))
    return out.decision_id


def reverse(ledger: Ledger, key: uuid.UUID) -> None:
    dispatch(
        "finance.decision.reverse",
        {"decision_id": str(key)},
        ledger.ctx.role,
        ctx=ledger.ctx,
    )


def line(ledger: Ledger, amount: str) -> uuid.UUID:
    found = ledger.conn.scalar(select(txn.c.id).where(txn.c.amount == Decimal(amount)))
    assert isinstance(found, uuid.UUID)
    return found


def ref(ledger: Ledger, amount: str) -> str:
    return format_ref("finance", "txn", line(ledger, amount))


def parser_change(
    monkeypatch: pytest.MonkeyPatch, *, shift: int = 0, amount_change: bool = False
) -> None:
    old = next(p for p in CARD_PARSERS if p.format == "txt")

    def changed(row: ParsedLine) -> ParsedLine:
        assert row.description_raw is not None
        assert row.posting_date is not None
        assert row.amount is not None
        return dataclasses.replace(
            row,
            description_raw=row.description_raw + " corrected",
            posting_date=row.posting_date + dt.timedelta(days=shift),
            amount=row.amount + Decimal("1.00") if amount_change else row.amount,
        )

    def parse(text: str, accounts: AccountLookup) -> ParseResult:
        result = old.parse(text, accounts)
        return dataclasses.replace(
            result,
            lines=tuple(changed(row) for row in result.lines),
        )

    monkeypatch.setitem(
        registry.PARSERS,
        old.parser_id,
        dataclasses.replace(old, version="2", parse=parse),
    )


def rebuild(ledger: Ledger) -> None:
    run(ledger.uow, ledger.ctx, extract_pdf=read_as_text, reparse=True)


def orphans(ledger: Ledger) -> set[tuple[uuid.UUID, uuid.UUID | None]]:
    return {
        (row.decision_id, row.candidate_txn_id)
        for row in ledger.conn.execute(
            select(decision_orphan.c.decision_id, decision_orphan.c.candidate_txn_id)
        )
    }


def test_rekeyed_category_and_pair_wait_for_an_answer_then_survive_rebuild(
    ledger: Ledger,
    card_parsers: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    setup(ledger)
    category = record(
        ledger,
        kind="category",
        target_ref=ref(ledger, "-9.99"),
        category="groceries",
        tag="personal",
    )
    paired = record(
        ledger,
        kind="pair",
        target_ref=ref(ledger, "-20.00"),
        counterpart_ref=ref(ledger, "20.00"),
    )
    ledger.derive()
    parser_change(monkeypatch)
    ledger.derive()  # The changed parser version invalidates the retained cache.
    purchase, debit, credit = (line(ledger, x) for x in ("-9.99", "-20.00", "20.00"))
    assert orphans(ledger) == {(category, purchase), (paired, debit), (paired, credit)}
    assert ledger.conn.scalar(select(func.count()).select_from(decision)) == 2
    assert set(ledger.conn.scalars(select(txn.c.orphan_candidate))) == {True}
    assert (
        ledger.conn.scalar(select(txn.c.category).where(txn.c.id == purchase)) is None
    )
    assert (
        ledger.conn.scalar(
            select(func.count())
            .select_from(pair_edge)
            .where(pair_edge.c.basis_ref == format_ref("finance", "decision", paired))
        )
        == 0
    )
    assert (
        ledger.conn.scalar(
            select(report_status.c.count).where(
                report_status.c.fact == "decision_orphan"
            )
        )
        == 2
    )

    restored = record(
        ledger,
        kind="category",
        target_ref=ref(ledger, "-9.99"),
        category="groceries",
        tag="personal",
        about_decision_id=category,
    )
    repaired = record(
        ledger,
        kind="pair",
        target_ref=ref(ledger, "-20.00"),
        counterpart_ref=ref(ledger, "20.00"),
        about_decision_id=paired,
    )
    for _ in range(2):
        rebuild(ledger)
        assert orphans(ledger) == set()
        assert set(ledger.conn.scalars(select(txn.c.orphan_candidate))) == {False}
        row = ledger.conn.execute(select(txn).where(txn.c.id == purchase)).one()
        assert (row.category, row.category_basis_class, row.category_basis_ref) == (
            "groceries",
            "owner",
            format_ref("finance", "decision", restored),
        )
        assert (row.tag, row.tag_basis_class) == ("personal", "owner")
        assert (
            ledger.conn.scalar(
                select(func.count())
                .select_from(pair_edge)
                .where(
                    pair_edge.c.applied,
                    pair_edge.c.basis_ref
                    == format_ref("finance", "decision", repaired),
                )
            )
            == 1
        )
    reverse(ledger, restored)
    reverse(ledger, repaired)
    rebuild(ledger)
    assert orphans(ledger) == {(category, purchase), (paired, debit), (paired, credit)}


def test_declining_a_candidate_clears_only_its_label_until_reversed(
    ledger: Ledger,
    card_parsers: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    setup(ledger)
    original = record(
        ledger, kind="category", target_ref=ref(ledger, "-9.99"), category="groceries"
    )
    parser_change(monkeypatch)
    ledger.derive()
    candidate = line(ledger, "-9.99")
    declined = record(
        ledger,
        kind="reattach_decline",
        target_ref=ref(ledger, "-9.99"),
        about_decision_id=original,
    )
    rebuild(ledger)
    assert orphans(ledger) == {(original, None)}
    assert not ledger.conn.scalar(
        select(txn.c.orphan_candidate).where(txn.c.id == candidate)
    )
    reverse(ledger, declined)
    rebuild(ledger)
    assert orphans(ledger) == {(original, candidate)}
    assert ledger.conn.scalar(
        select(txn.c.orphan_candidate).where(txn.c.id == candidate)
    )


@pytest.mark.parametrize(("shift", "changed_amount"), [(4, False), (0, True)])
def test_an_orphan_beyond_the_snapshot_tolerance_remains_listed(
    ledger: Ledger,
    card_parsers: None,
    monkeypatch: pytest.MonkeyPatch,
    shift: int,
    changed_amount: bool,
) -> None:
    setup(ledger)
    original = record(
        ledger, kind="category", target_ref=ref(ledger, "-9.99"), category="groceries"
    )
    parser_change(monkeypatch, shift=shift, amount_change=changed_amount)
    rebuild(ledger)
    assert orphans(ledger) == {(original, None)}
    assert not any(ledger.conn.scalars(select(txn.c.orphan_candidate)))
    assert ledger.conn.scalar(select(func.count()).select_from(decision)) == 1


def test_a_timezone_change_preserves_stored_line_keys(
    ledger: Ledger,
    card_parsers: None,
) -> None:
    setup(ledger)
    record(
        ledger, kind="category", target_ref=ref(ledger, "-9.99"), category="groceries"
    )
    record(
        ledger,
        kind="pair",
        target_ref=ref(ledger, "-20.00"),
        counterpart_ref=ref(ledger, "20.00"),
    )
    before = {ref(ledger, x) for x in ("-9.99", "-20.00", "20.00")}
    ledger.ctx = dataclasses.replace(
        ledger.ctx,
        settings=parse_settings(
            f'[finance.paths]\narchive = "{ledger.archive}"\n'
            '[finance.locale]\ntimezone = "Pacific/Auckland"\n'
        ),
    )
    rebuild(ledger)
    assert {ref(ledger, x) for x in ("-9.99", "-20.00", "20.00")} == before
    assert orphans(ledger) == set()


def test_another_unreversed_category_blocks_the_candidate(
    ledger: Ledger,
    card_parsers: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    setup(ledger)
    original = record(
        ledger, kind="category", target_ref=ref(ledger, "-9.99"), category="groceries"
    )
    parser_change(monkeypatch)
    ledger.derive()
    other = record(
        ledger, kind="category", target_ref=ref(ledger, "-9.99"), category="fuel"
    )
    rebuild(ledger)
    assert orphans(ledger) == {(original, None)}
    assert not any(ledger.conn.scalars(select(txn.c.orphan_candidate)))
    reverse(ledger, other)
    rebuild(ledger)
    assert orphans(ledger) == {(original, line(ledger, "-9.99"))}


@pytest.mark.parametrize("kind", ["presumed_paid", "obligation_dispute"])
def test_an_obligation_key_that_disappears_is_listed_until_reversed(
    ledger: Ledger,
    kind: str,
) -> None:
    catalogue.load_all()
    ledger.manual("payees.csv", "sample-service,Sample Service,vendor,,,")
    ledger.manual(
        "obligations.csv", "sample-service,bill,,SS-999,60.00,,2026-02-20,,2026-02-01"
    )
    ledger.derive()
    key = ledger.conn.scalar(select(obligation.c.id))
    assert isinstance(key, uuid.UUID)
    original = record(
        ledger, kind=kind, obligation_ref=format_ref("finance", "obligation", key)
    )
    ledger.derive()
    assert orphans(ledger) == set()
    ledger.manual("obligations.csv")
    rebuild(ledger)
    assert orphans(ledger) == {(original, None)}
    assert (
        ledger.conn.scalar(
            select(report_status.c.count).where(
                report_status.c.fact == "decision_orphan"
            )
        )
        == 1
    )
    reverse(ledger, original)
    rebuild(ledger)
    assert orphans(ledger) == set()


def test_a_missing_pair_leg_makes_the_whole_answer_inert(
    ledger: Ledger,
    card_parsers: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    setup(ledger)
    original = record(
        ledger,
        kind="pair",
        target_ref=ref(ledger, "20.00"),
        legs=[{"target_ref": ref(ledger, "-20.00")}],
    )
    old = next(p for p in CARD_PARSERS if p.format == "txt")

    def parse(text: str, accounts: AccountLookup) -> ParseResult:
        result = old.parse(text, accounts)
        return dataclasses.replace(
            result,
            lines=tuple(
                dataclasses.replace(row, description_raw="Sample corrected debit")
                if row.amount == Decimal("-20.00")
                else row
                for row in result.lines
            ),
        )

    monkeypatch.setitem(
        registry.PARSERS,
        old.parser_id,
        dataclasses.replace(old, version="2", parse=parse),
    )
    rebuild(ledger)
    assert {key for key, _ in orphans(ledger)} == {original}
    assert (original, line(ledger, "-20.00")) in orphans(ledger)
    assert (
        ledger.conn.scalar(
            select(func.count())
            .select_from(pair_edge)
            .where(pair_edge.c.basis_ref == format_ref("finance", "decision", original))
        )
        == 0
    )


def test_candidates_match_account_currency_and_inclusive_posting_tolerance(
    ledger: Ledger,
    card_parsers: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    setup(ledger)
    original = record(
        ledger, kind="category", target_ref=ref(ledger, "-9.99"), category="groceries"
    )
    ledger.manual(
        "accounts.csv",
        "card,Sample Card,Sample Card,credit_card,CAD;USD,monthly,,,,,,,,,",
        "other,Sample Other,Sample Card,chequing,CAD,monthly,,,,,,,,,",
    )
    ledger.cash(
        "other,2026-02-02,-9.99,CAD,Sample other account,",
        "card,2026-02-02,-9.99,USD,Sample other currency,",
        "card,2026-01-30,-9.994,CAD,Sample nearby purchase,",
    )
    parser_change(monkeypatch, shift=3)
    ledger.derive()
    candidates = set(
        ledger.conn.scalars(select(txn.c.id).where(txn.c.orphan_candidate))
    )
    # Both posting-day boundaries are inclusive, and native precision compares
    # at the cent. Neither another account nor another currency is a candidate.
    assert len(candidates) == 2
    assert orphans(ledger) == {(original, candidate) for candidate in candidates}
    rows = ledger.conn.execute(select(txn).where(txn.c.id.in_(candidates))).all()
    assert {row.currency for row in rows} == {"CAD"}
    assert {row.posting_date for row in rows} == {
        dt.date(2026, 1, 30),
        dt.date(2026, 2, 5),
    }
