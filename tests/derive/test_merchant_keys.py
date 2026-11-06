"""Merchant privacy and orphan lifecycle through derive and public operations.

Every printed name, date and amount is synthetic.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import uuid
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.pool import NullPool

from conftest import DatabaseUnderTest
from rheo_finance import catalogue
from rheo_finance.contracts import Role
from rheo_finance.derive.snapshot import txn_snapshot
from rheo_finance.operations import dispatch, with_databases, with_owner_connection
from rheo_finance.reports.open_items import ListIn, ListOut, list_items
from rheo_finance.storage.refs import format_ref
from rheo_finance.storage.schema import decision_orphan

from .support import Ledger
from .test_orphans import rebuild, record, reverse


def prepare(ledger: Ledger, *rows: str) -> None:
    catalogue.load_all()
    ledger.card()
    ledger.cash(*rows)
    ledger.ctx = dataclasses.replace(ledger.ctx, snapshot_provider=txn_snapshot)
    ledger.derive()


def by_description(ledger: Ledger, text: str) -> dict[str, object]:
    return next(
        row for row in ledger.txns() if row["description_norm"] == text.casefold()
    )


def target(row: dict[str, object]) -> str:
    key = row["id"]
    assert isinstance(key, uuid.UUID)
    return format_ref("finance", "txn", key)


def test_registered_payees_share_keys_and_every_label_withholds_printed_names(
    ledger: Ledger,
) -> None:
    descriptions = [
        *(f"E-TRANSFER SENT Sample Cedar {n}" for n in range(4)),
        *(f"INTERAC e-Transfer from Sample Birch {n}" for n in range(2)),
        "EMT TRANSFER - DEBIT PAYEE: Sample Maple",
    ]
    ledger.manual(
        "payees.csv",
        "housing,Housing,person,Sample Cedar,,",
        "connection,Connection,vendor,Sample Birch,,",
    )
    prepare(ledger, *(f"card,2026-02-02,-11.00,CAD,{text}," for text in descriptions))
    rows = [by_description(ledger, text) for text in descriptions]
    assert {row["merchant_key"] for row in rows[:4]} == {"etransfer:housing:out"}
    assert {row["display_label"] for row in rows[:4]} == {"Housing"}
    assert {row["merchant_key"] for row in rows[4:6]} == {"etransfer:connection:out"}
    assert {row["display_label"] for row in rows[4:6]} == {"Connection"}
    assert rows[-1]["merchant_key"] == f"etransfer:out:{rows[-1]['id']}"
    assert rows[-1]["display_label"] == "e-Transfer (name withheld)"
    for row in ledger.txns():
        safe = f"{row['merchant_key']} {row['display_label']}".casefold()
        assert not any(name in safe for name in ("cedar", "birch", "maple"))
    before = ledger.txns()
    rebuild(ledger)
    assert ledger.txns() == before


@pytest.mark.parametrize(
    "wording",
    [
        "E-TRANSFER FROM",
        "PAYEE:",
        "PAYER:",
        "TRANSFER TO",
        "TRANSFER FROM",
        "E-TRANSFER",
        "INTERAC",
        "EMT TRANSFER",
        "etransfer debit (",
        "e-transfer returned (",
    ],
)
def test_each_reference_wording_withholds_an_unregistered_name(
    ledger: Ledger,
    wording: str,
) -> None:
    description = f"{wording} Sample Spruce)"
    prepare(ledger, f"card,2026-02-02,11.00,CAD,{description},")
    (row,) = ledger.txns()
    assert row["merchant_key"] == f"etransfer:in:{row['id']}"
    assert row["display_label"] == "e-Transfer (name withheld)"


@pytest.mark.parametrize(
    "description,matched",
    [
        ("E-TRANSFER to sample-cedar", True),
        ("E-TRANSFER to Sample Cedars", False),
        ("Sample Cedar E-TRANSFER to Sample Maple", False),
        ("etransfer debit (Sample Cedar)", True),
    ],
)
def test_payee_matching_is_whole_token_and_only_in_the_name_position(
    ledger: Ledger,
    description: str,
    matched: bool,
) -> None:
    ledger.manual("payees.csv", "housing,Housing,person,Sample Cedar,,")
    prepare(ledger, f"card,2026-02-02,-11.00,CAD,{description},")
    (row,) = ledger.txns()
    assert row["merchant_key"] == (
        "etransfer:housing:out" if matched else f"etransfer:out:{row['id']}"
    )


def test_registered_patterns_and_counterparty_are_stripped_everywhere(
    ledger: Ledger,
    card_parsers: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from rheo_finance.parsers import registry
    from rheo_finance.parsers.base import AccountLookup, ParseResult

    from .support import CARD_IDENTIFIER, CARD_PARSERS

    parser = next(p for p in CARD_PARSERS if p.format == "txt")

    def parse(text: str, accounts: AccountLookup) -> ParseResult:
        result = parser.parse(text, accounts)
        return dataclasses.replace(
            result,
            lines=tuple(
                dataclasses.replace(row, counterparty="Sample Willow")
                for row in result.lines
            ),
        )

    monkeypatch.setitem(
        registry.PARSERS, parser.parser_id, dataclasses.replace(parser, parse=parse)
    )
    ledger.manual("payees.csv", "housing,Housing,person,Sample Cedar,,")
    ledger.card()
    ledger.file(
        "synthetic-card/lines.txt",
        "\n".join(
            [
                "SYNTHETIC CARD TRANSACTIONS",
                f"ACCOUNT {CARD_IDENTIFIER}",
                "LINE 2026-02-02 - -11.00 Market Sample Cedar sample-cedar "
                "Sample Willow SAMPLE WILLOW",
            ]
        ),
    )
    ledger.derive()
    (row,) = ledger.txns()
    assert row["merchant_key"] == "market"
    assert row["display_label"] == "Market"


def test_line_answer_survives_regrouping_and_outranks_a_newer_unit_answer(
    ledger: Ledger,
) -> None:
    prepare(
        ledger,
        "card,2026-02-02,-11.00,CAD,E-TRANSFER Sample Cedar,",
        "card,2026-02-03,-12.00,CAD,E-TRANSFER Sample Cedar,",
        "card,2026-02-04,13.00,CAD,E-TRANSFER Sample Cedar,",
    )
    first = by_description(ledger, "E-TRANSFER Sample Cedar")
    original = record(
        ledger,
        kind="category",
        target_ref=target(first),
        category="line-only",
        tag="personal",
    )
    ledger.manual("payees.csv", "housing,Housing,person,Sample Cedar,,")
    ledger.derive()
    rows = ledger.txns()
    unit = record(
        ledger,
        kind="category",
        merchant_key="etransfer:housing:out",
        newest_line_ref=target(rows[1]),
        category="unit",
        tag="business",
        business_unit="other",
    )
    rebuild(ledger)
    first, second, incoming = ledger.txns()
    assert (first["category"], first["tag"]) == ("line-only", "personal")
    assert first["category_basis_ref"] == format_ref("finance", "decision", original)
    assert (second["category"], second["tag"]) == ("unit", "business")
    assert second["category_basis_ref"] == format_ref("finance", "decision", unit)
    assert first["category_basis_class"] == second["tag_basis_class"] == "owner"
    assert incoming["merchant_key"] == "etransfer:housing:in"
    assert incoming["category"] is None
    reverse(ledger, original)
    rebuild(ledger)
    assert ledger.txns()[0]["category"] == "unit"
    reverse(ledger, unit)
    rebuild(ledger)
    assert all(row["category"] is None for row in ledger.txns())


def list_orphans(ledger: Ledger) -> ListOut:
    return list_items(ledger.ctx, ledger.uow, ListIn(kind="orphaned_decision"))


def merchant_orphan(ledger: Ledger) -> uuid.UUID:
    prepare(ledger, "card,2026-02-02,-11.00,CAD,Sample Store,")
    (row,) = ledger.txns()
    original = record(
        ledger,
        kind="category",
        merchant_key="sample store",
        newest_line_ref=target(row),
        category="groceries",
        tag="personal",
    )
    ledger.cash(
        "card,2026-02-01,-11.00,CAD,Sample Market,",
        "card,2026-02-03,-11.00,CAD,Sample Shop,",
        "card,2026-02-03,-11.00,CAD,Sample Market,",
        "card,2026-02-02,-12.00,CAD,Sample Other,",
        "card,2026-03-02,-11.00,CAD,Sample Far,",
    )
    ledger.derive()
    return original


def test_merchant_orphan_candidates_preserve_one_snapshot_money_row_and_safe_refs(
    ledger: Ledger,
) -> None:
    original = merchant_orphan(ledger)
    for _ in range(2):
        stored = list(ledger.conn.execute(select(decision_orphan)))
        assert len(stored) == 3
        assert {row.decision_id for row in stored} == {original}
        assert {row.candidate_merchant_key for row in stored} == {
            "sample market",
            "sample shop",
        }
        result = list_orphans(ledger)
        (row,), (summary,) = result.items, result.summary
        assert row.line_ref == format_ref("finance", "decision", original)
        assert row.candidate_count == 3
        assert row.candidate_refs == sorted(
            format_ref("finance", "txn", candidate.candidate_txn_id)
            for candidate in stored
        )
        assert (row.occurred_on, row.amount, row.category, row.tag) == (
            dt.date(2026, 2, 2),
            Decimal("-11.00"),
            "groceries",
            "personal",
        )
        assert summary.count == 1 and summary.sums[0].out_amount == Decimal("11.00")
        public = result.model_dump_json()
        assert "merchant_key" not in public
        assert not any(text in public for text in ("sample market", "sample shop"))
        assert all(row["category"] is None for row in ledger.txns())
        rebuild(ledger)


@pytest.mark.parametrize("coverage", ["merchant", "line"])
def test_active_owner_answers_block_candidates_and_reversal_reopens_them(
    ledger: Ledger,
    coverage: str,
) -> None:
    merchant_orphan(ledger)
    candidate = by_description(ledger, "Sample Shop")
    fields = (
        {
            "merchant_key": candidate["merchant_key"],
            "newest_line_ref": target(candidate),
        }
        if coverage == "merchant"
        else {"target_ref": target(candidate)}
    )
    answer = record(ledger, kind="category", category="fuel", **fields)
    rebuild(ledger)
    (row,) = list_orphans(ledger).items
    assert row.candidate_count == 2 and target(candidate) not in row.candidate_refs
    reverse(ledger, answer)
    rebuild(ledger)
    (row,) = list_orphans(ledger).items
    assert row.candidate_count == 3 and target(candidate) in row.candidate_refs


def test_merchant_answer_reattaches_only_a_matching_uncovered_snapshot(
    ledger: Ledger,
) -> None:
    original = merchant_orphan(ledger)
    wrong = by_description(ledger, "Sample Other")
    unrelated = record(
        ledger,
        kind="category",
        merchant_key=wrong["merchant_key"],
        newest_line_ref=target(wrong),
        category="fuel",
        about_decision_id=original,
    )
    rebuild(ledger)
    assert len(list_orphans(ledger).items) == 1
    reverse(ledger, unrelated)
    replacement = by_description(ledger, "Sample Shop")
    answer = record(
        ledger,
        kind="category",
        merchant_key=replacement["merchant_key"],
        newest_line_ref=target(replacement),
        category="groceries",
        about_decision_id=original,
    )
    rebuild(ledger)
    assert not list_orphans(ledger).items
    assert by_description(ledger, "Sample Shop")["category"] == "groceries"
    ledger.cash("card,2026-02-03,-11.00,CAD,Sample New Shop,")
    rebuild(ledger)
    (row,) = list_orphans(ledger).items
    assert row.line_ref == format_ref("finance", "decision", answer)
    reverse(ledger, answer)
    rebuild(ledger)
    (row,) = list_orphans(ledger).items
    assert row.line_ref == format_ref("finance", "decision", original)


def test_merchant_is_present_while_any_current_line_carries_its_key(
    ledger: Ledger,
) -> None:
    prepare(
        ledger,
        "card,2026-02-02,-11.00,CAD,Sample Store,",
        "card,2026-02-03,-12.00,CAD,Sample Store,",
    )
    record(
        ledger,
        kind="category",
        merchant_key="sample store",
        newest_line_ref=target(ledger.txns()[1]),
        category="groceries",
    )
    ledger.cash("card,2026-02-02,-11.00,CAD,Sample Store,")
    rebuild(ledger)
    assert not list_orphans(ledger).items
    assert ledger.txns()[0]["category"] == "groceries"


def test_field_precedence_is_independent_for_line_and_merchant_answers(
    ledger: Ledger,
) -> None:
    prepare(ledger, "card,2026-02-02,-11.00,CAD,Sample Store,")
    (row,) = ledger.txns()
    specific = record(ledger, kind="category", target_ref=target(row), category="line")
    general = record(
        ledger,
        kind="category",
        merchant_key="sample store",
        newest_line_ref=target(row),
        category="merchant",
        tag="personal",
    )
    ledger.derive()
    (row,) = ledger.txns()
    assert (row["category"], row["tag"]) == ("line", "personal")
    assert row["category_basis_ref"] == format_ref("finance", "decision", specific)
    assert row["tag_basis_ref"] == format_ref("finance", "decision", general)


def test_merchant_decline_and_orphan_reversal_survive_rebuild(ledger: Ledger) -> None:
    original = merchant_orphan(ledger)
    candidate = by_description(ledger, "Sample Shop")
    declined = record(
        ledger,
        kind="reattach_decline",
        target_ref=target(candidate),
        about_decision_id=original,
    )
    rebuild(ledger)
    assert target(candidate) not in list_orphans(ledger).items[0].candidate_refs
    reverse(ledger, declined)
    rebuild(ledger)
    assert target(candidate) in list_orphans(ledger).items[0].candidate_refs
    ledger.cash("card,2026-03-02,-11.00,CAD,Sample Far,")
    rebuild(ledger)
    (row,) = list_orphans(ledger).items
    assert row.candidate_count == 0 and row.candidate_refs == []
    assert row.amount == Decimal("-11.00") and "no replacement candidate" in row.reason
    reverse(ledger, original)
    rebuild(ledger)
    assert not list_orphans(ledger).items


def test_a_merchant_answer_cannot_reattach_using_another_units_newest_line(
    ledger: Ledger,
) -> None:
    original = merchant_orphan(ledger)
    shop = by_description(ledger, "Sample Shop")
    record(
        ledger,
        kind="category",
        merchant_key="sample other",
        newest_line_ref=target(shop),
        category="fuel",
        about_decision_id=original,
    )
    rebuild(ledger)
    assert len(list_orphans(ledger).items) == 1


def test_reader_operation_keeps_merchant_candidates_private_and_money_single(
    ledger: Ledger,
    scratch_database: DatabaseUnderTest,
    tmp_path: Path,
) -> None:
    # Commit only to the disposable scratch database so the actual reader DSN can
    # see the fixture. The shared finance_test fixture is always rolled back.
    folder = tmp_path / "reader"
    folder.mkdir()
    engine = create_engine(scratch_database.owner_url, poolclass=NullPool)
    try:
        with engine.begin() as conn:
            ctx = with_databases(
                ledger.ctx,
                owner_url=scratch_database.owner_url,
                reader_url=scratch_database.reader_url,
            )
            fixture = Ledger(conn, with_owner_connection(ctx, conn), folder)
            original = merchant_orphan(fixture)
        ctx = with_databases(
            dataclasses.replace(fixture.ctx, role=Role.MEMBER),
            owner_url=scratch_database.owner_url.set(
                database="finance_unavailable_owner_test"
            ),
            reader_url=scratch_database.reader_url,
        )
        out = dispatch(
            "finance.openitem.list", {"kind": "orphaned_decision"}, Role.MEMBER, ctx=ctx
        )
        assert isinstance(out, ListOut)
        (row,), (summary,) = out.items, out.summary
        assert row.line_ref == format_ref("finance", "decision", original)
        assert row.candidate_count == len(row.candidate_refs) == 3
        assert all(ref.startswith("finance.txn:") for ref in row.candidate_refs)
        assert summary.count == 1 and summary.sums[0].out_amount == Decimal("11.00")
        assert "merchant_key" not in out.model_dump_json()
        assert "sample market" not in out.model_dump_json()
        assert "sample shop" not in out.model_dump_json()
    finally:
        engine.dispose()
