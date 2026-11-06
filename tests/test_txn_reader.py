"""Populated line privacy, cardinality and filters over the real reader DSN."""

from __future__ import annotations

import datetime as dt
import hashlib
import uuid
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import create_engine, delete, insert, select, text, update
from sqlalchemy.pool import NullPool

from conftest import DatabaseUnderTest
from rheo_finance import catalogue, operations
from rheo_finance.clock import FakeClock
from rheo_finance.context import FinanceContext, FinancePaths
from rheo_finance.contracts import Role
from rheo_finance.reports.lines import ListOut
from rheo_finance.settings import parse_settings
from rheo_finance.storage.ids import derived_id
from rheo_finance.storage.schema import (
    account,
    decision,
    document,
    document_line,
    obligation,
    payee,
    report_crypto_movement,
    txn,
    txn_source,
)
from test_reader_grants import REPORT_VIEWS

DAY = dt.date(2028, 2, 3)
NOW = dt.datetime(2028, 2, 4, tzinfo=dt.UTC)
SECRET = "Synthetic Alder"
IDENTIFIER = "999000000077"


def key(label: str) -> uuid.UUID:
    return derived_id(DAY, ("synthetic_reader", label))


def ref(label: str) -> str:
    return f"finance.txn:{key(label)}"


@pytest.fixture(scope="module")
def seeded(scratch_database: DatabaseUnderTest) -> Iterator[DatabaseUnderTest]:
    engine = create_engine(scratch_database.owner_url, poolclass=NullPool)
    with engine.begin() as conn:
        for slug in ("sample-a", "sample-b"):
            conn.execute(
                insert(account).values(
                    id=key(slug),
                    slug=slug,
                    label=slug.title(),
                    institution="Sample",
                    kind="chequing",
                    currencies=["CAD", "BTC"],
                    cadence="monthly",
                    sourced_by="documents",
                )
            )
        conn.execute(
            insert(document).values(
                id=key("doc"),
                path="synthetic/reader.txt",
                kind="statement",
                issuer="synthetic",
                format="txt",
                status="parsed",
                imported_at=NOW,
                sha256=hashlib.sha256(b"synthetic reader").hexdigest(),
            )
        )
        for n, label in enumerate(("one", "two", "three", "crypto"), 1):
            printed = (SECRET, "SYNTHETIC   ALDER", "synthetic\talder", SECRET)[n - 1]
            conn.execute(
                insert(txn).values(
                    id=key(label),
                    account_id=key("sample-b" if n == 3 else "sample-a"),
                    posting_date=DAY + dt.timedelta(days=n - 1),
                    txn_date=DAY,
                    recognition_date=DAY,
                    amount=Decimal("-9.999999999"),
                    currency="BTC" if label == "crypto" else "CAD",
                    cad_amount=Decimal("-10.00"),
                    basis="statement",
                    origin="document_line",
                    movement_kind="crypto_transfer" if label == "crypto" else "fiat",
                    display_label=(
                        None if label == "crypto" else "e-Transfer (name withheld)"
                    ),
                    description_norm=f"Interac e-Transfer from {printed} {IDENTIFIER}",
                    merchant_key=f"hidden {SECRET}",
                    counterparty=printed,
                    txid="synthetic-hidden-txid",
                    from_address="synthetic-hidden-address",
                    category="groceries",
                    category_basis_class="rule",
                    tag="split",
                    business_unit="sample",
                    business_pct=Decimal("25"),
                    tag_basis_class="model-auto",
                    orphan_candidate=(n == 1),
                    pair_kind="reversal" if n == 1 else None,
                    transfer_state="paired" if n == 1 else "unresolved",
                    provisional=(n == 2),
                )
            )
            for role in ("primary", "additional"):
                source = key(f"{label}-{role}")
                conn.execute(
                    insert(document_line).values(
                        id=source,
                        document_id=key("doc"),
                        line_no=2 * n + (role == "primary"),
                        line_kind="transaction",
                        posting_date=DAY,
                        account_id=key("sample-a"),
                        amount=Decimal("-9.999999999"),
                        currency="CAD",
                        line_key_id=source,
                        description_raw=printed,
                    )
                )
                conn.execute(
                    insert(txn_source).values(
                        txn_id=key(label),
                        document_line_id=source,
                        line_key_id=source,
                        role=role,
                    )
                )
        for label in ("first", "second"):
            conn.execute(
                insert(decision).values(
                    id=key(label),
                    kind="split",
                    target_ref=ref("one-additional"),
                    decided_at=NOW,
                    bulk_id=key("bulk") if label == "second" else None,
                )
            )
    yield scratch_database
    engine.dispose()


@pytest.fixture
def ctx(seeded: DatabaseUnderTest, tmp_path: Path) -> FinanceContext:
    catalogue.load_all()
    context = FinanceContext(
        settings=parse_settings(""),
        clock=FakeClock(NOW),
        paths=FinancePaths(data=tmp_path),
        role=Role.MEMBER,
        snapshot_provider=lambda _uow, _ref: None,
    )
    return operations.with_databases(
        context, owner_url=seeded.owner_url.set(port=1), reader_url=seeded.reader_url
    )


def lines(ctx: FinanceContext, **filters: object) -> ListOut:
    out = operations.dispatch("finance.txn.list", filters, Role.MEMBER, ctx=ctx)
    assert isinstance(out, ListOut)
    return out


def test_lines_keep_precision_basis_annotations_and_one_row_per_line(
    ctx: FinanceContext,
) -> None:
    out = lines(ctx)
    assert len(out.rows) == 4
    one = out.rows[0]
    assert one.line_ref == ref("one-primary")
    assert one.amount == Decimal("-9.999999999")
    assert one.cad_amount == Decimal("-10.00")
    assert one.tag_basis_class == "model-auto" and one.category_basis_class == "rule"
    assert one.business_pct == Decimal("25")
    assert one.decision_state == "decision orphaned"
    assert one.split_state == "split kept, on a reversed line"
    assert all(
        row.display_label == "e-Transfer (name withheld)" for row in out.rows[:3]
    )
    assert out.rows[3].display_label == "Transaction"
    assert out.rows[1].provisional is True
    assert all(row.split_state is None for row in out.rows[1:])


def test_named_filters_are_inclusive_and_combine(ctx: FinanceContext) -> None:
    assert len(lines(ctx, account="sample-a").rows) == 3
    assert len(lines(ctx, state="unresolved").rows) == 3
    assert len(lines(ctx, state="decision orphaned").rows) == 1
    got = lines(
        ctx, account="sample-a", date_from=str(DAY), date_to=str(DAY), state="reversal"
    )
    assert [row.line_ref for row in got.rows] == [ref("one-primary")]
    assert lines(ctx, account="missing").rows == []
    assert lines(ctx, state="missing").rows == []
    with pytest.raises(operations.InputInvalid):
        lines(ctx, date_from="2028-02-04", date_to="2028-02-03")


def test_every_populated_reader_view_and_line_output_withholds_restricted_text(
    seeded: DatabaseUnderTest,
    ctx: FinanceContext,
) -> None:
    engine = create_engine(seeded.reader_url, poolclass=NullPool)
    try:
        with engine.connect() as conn:
            for view in sorted(REPORT_VIEWS):
                shown = repr(conn.execute(text(f"SELECT * FROM finance.{view}")).all())
                for secret in (
                    SECRET,
                    IDENTIFIER,
                    "synthetic-hidden-txid",
                    "synthetic-hidden-address",
                ):
                    assert "".join(secret.casefold().split()) not in "".join(
                        shown.casefold().split()
                    ), view
            aggregate = conn.execute(select(report_crypto_movement)).mappings().one()
            assert aggregate["count"] == 1
            assert aggregate["amount"] == Decimal("-9.999999999")
    finally:
        engine.dispose()
    shown_output = "".join(lines(ctx).model_dump_json().casefold().split())
    assert "".join(SECRET.casefold().split()) not in shown_output
    assert IDENTIFIER not in shown_output


def test_reversed_split_decisions_do_not_leave_the_annotation(
    seeded: DatabaseUnderTest,
    ctx: FinanceContext,
) -> None:
    engine = create_engine(seeded.owner_url, poolclass=NullPool)
    try:
        with engine.connect() as conn:
            # The annotation is read on the actual reader DSN after both kinds of
            # reversal are committed. The scratch database is dropped after module.
            for label, values in (
                ("reverse-one", {"reverses_id": key("first")}),
                ("reverse-bulk", {"reverses_bulk_id": key("bulk")}),
            ):
                conn.execute(
                    insert(decision).values(
                        id=key(label), kind="reverse", decided_at=NOW, **values
                    )
                )
            conn.commit()
        assert lines(ctx).rows[0].split_state is None
    finally:
        engine.dispose()


def test_crypto_groups_keep_pair_kind_units_null_states_and_unknown_cad(
    seeded: DatabaseUnderTest,
) -> None:
    owner = create_engine(seeded.owner_url, poolclass=NullPool)
    reader = create_engine(seeded.reader_url, poolclass=NullPool)
    ids: list[uuid.UUID] = []
    try:
        with owner.begin() as conn:
            base = dict(
                conn.execute(select(txn).where(txn.c.id == key("crypto")))
                .mappings()
                .one()
            )
            base.pop("tax_year")
            for n, (currency, kind, pair, state, amount, cad, exclusion) in enumerate(
                [
                    (
                        "BTC",
                        "crypto_transfer",
                        "own_one_sided",
                        "paired",
                        "-0.2",
                        "-2.01",
                        None,
                    ),
                    (
                        "BTC",
                        "crypto_transfer",
                        "own_transfer",
                        "paired",
                        "-0.3",
                        "-3.02",
                        None,
                    ),
                    (
                        "BTC",
                        "crypto_transfer",
                        "own_transfer",
                        "paired",
                        "0.1",
                        None,
                        None,
                    ),
                    (
                        "ETH",
                        "crypto_transfer",
                        "own_transfer",
                        "paired",
                        "-0.7",
                        "-7.00",
                        None,
                    ),
                    ("BTC", "crypto_fee", None, None, "-0.01", "-0.10", None),
                    (
                        "BTC",
                        "crypto_transfer",
                        "own_one_sided",
                        "paired",
                        "-99",
                        "-99",
                        "spam",
                    ),
                ]
            ):
                identity = key(f"crypto-group-{n}")
                ids.append(identity)
                conn.execute(
                    insert(txn).values(
                        {
                            **base,
                            "id": identity,
                            "currency": currency,
                            "movement_kind": kind,
                            "pair_kind": pair,
                            "transfer_state": state,
                            "amount": Decimal(amount),
                            "cad_amount": None if cad is None else Decimal(cad),
                            "exclusion": exclusion,
                        }
                    )
                )
                conn.execute(
                    insert(txn_source).values(
                        txn_id=identity,
                        manual_ref=f"synthetic-group-{n}",
                        line_key_id=identity,
                        role="primary",
                    )
                )
        with reader.connect() as conn:
            groups = {
                (
                    row["currency"],
                    row["movement_kind"],
                    row["state"],
                    row["pair_kind"],
                ): (row["count"], row["amount"], row["cad_amount"])
                for row in conn.execute(select(report_crypto_movement)).mappings()
            }
        assert groups[("BTC", "crypto_transfer", "paired", "own_one_sided")] == (
            1,
            Decimal("-0.2"),
            Decimal("-2.01"),
        )
        assert groups[("BTC", "crypto_transfer", "paired", "own_transfer")] == (
            2,
            Decimal("-0.2"),
            None,
        )
        assert groups[("ETH", "crypto_transfer", "paired", "own_transfer")] == (
            1,
            Decimal("-0.7"),
            Decimal("-7.00"),
        )
        assert groups[("BTC", "crypto_fee", None, None)] == (
            1,
            Decimal("-0.01"),
            Decimal("-0.10"),
        )
        assert len(groups) == 5  # Includes the original unresolved crypto line.
    finally:
        with owner.begin() as conn:
            conn.execute(delete(txn_source).where(txn_source.c.txn_id.in_(ids)))
            conn.execute(delete(txn).where(txn.c.id.in_(ids)))
        owner.dispose()
        reader.dispose()


def test_owed_patterns_disclose_only_empty_versus_nonempty(
    seeded: DatabaseUnderTest,
) -> None:
    from test_owed_ops import add

    owner = create_engine(seeded.owner_url, poolclass=NullPool)
    reader = create_engine(seeded.reader_url, poolclass=NullPool)
    try:
        with owner.begin() as conn:
            add(conn, slug="sample-privacy")
        for patterns in ([], [SECRET], [SECRET, "second-secret-pattern"]):
            with owner.begin() as conn:
                conn.execute(
                    update(payee)
                    .where(payee.c.slug == "sample-privacy")
                    .values(patterns=patterns)
                )
            with reader.connect() as conn:
                for view in ("report_owed", "report_owed_trace"):
                    rows = (
                        conn.execute(
                            text(
                                f"SELECT * FROM finance.{view} "
                                "WHERE payee_slug = 'sample-privacy'"
                            )
                        )
                        .mappings()
                        .all()
                    )
                    assert len(rows) == 1
                    assert rows[0]["payments_unmatchable"] is (not patterns)
                    shown = "".join(repr(rows).casefold().split())
                    assert "".join(SECRET.casefold().split()) not in shown
                    assert "second-secret-pattern" not in shown
    finally:
        with owner.begin() as conn:
            pid = conn.scalar(
                select(payee.c.id).where(payee.c.slug == "sample-privacy")
            )
            conn.execute(delete(obligation).where(obligation.c.payee_id == pid))
            conn.execute(delete(payee).where(payee.c.id == pid))
        owner.dispose()
        reader.dispose()
