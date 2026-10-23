"""The database rules of ``txn`` and ``txn_source`` (AC-14, FR-13): a txn with no
source cannot exist, a line key and a source line each feed one txn, and a txn has one
primary source. The deferred checks fire at ``SET CONSTRAINTS ALL IMMEDIATE``, which is
what a commit does. Every value is synthetic.
"""

from __future__ import annotations

import datetime as dt
import uuid
from decimal import Decimal

import pytest
from sqlalchemy import Connection, delete, insert, select, text
from sqlalchemy.exc import IntegrityError

from rheo_finance.storage.ids import derived_id
from rheo_finance.storage.schema import txn, txn_source

from .support import D, Ledger


def new_id(name: str) -> uuid.UUID:
    return derived_id(D(2026, 2, 1), ("constraint test", name))


def add_txn(ledger: Ledger, name: str, **values: object) -> uuid.UUID:
    txn_id = new_id(name)
    account_id = ledger.conn.scalar(text("SELECT id FROM finance.account LIMIT 1"))
    row = {
        "id": txn_id,
        "account_id": account_id,
        "posting_date": D(2026, 2, 1),
        "amount": Decimal("-1.00"),
        "currency": "CAD",
        "origin": "manual",
        "movement_kind": "fiat",
    }
    ledger.conn.execute(insert(txn).values(**{**row, **values}))
    return txn_id


def add_source(
    ledger: Ledger,
    txn_id: uuid.UUID,
    ref: str,
    key: str | None = None,
    role: str = "primary",
) -> None:
    ledger.conn.execute(
        insert(txn_source).values(
            txn_id=txn_id,
            manual_ref=ref,
            line_key_id=new_id(key or ref),
            role=role,
        )
    )


def check(conn: Connection) -> None:
    """What a commit does with the deferred constraints, inside a savepoint."""
    with conn.begin_nested():
        conn.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
    conn.execute(text("SET CONSTRAINTS ALL DEFERRED"))


@pytest.fixture
def registered(ledger: Ledger) -> Ledger:
    ledger.chequing()
    ledger.derive()  # loads the account registry; no documents, no lines
    return ledger


def test_a_txn_with_no_source_fails_at_commit(registered: Ledger) -> None:
    add_txn(registered, "orphan")

    with pytest.raises(IntegrityError, match="has no primary source"):
        check(registered.conn)


def test_a_txn_with_only_an_additional_source_fails_at_commit(
    registered: Ledger,
) -> None:
    txn_id = add_txn(registered, "no primary")
    add_source(registered, txn_id, "manual:transactions.csv:2", role="additional")

    with pytest.raises(IntegrityError, match="has no primary source"):
        check(registered.conn)


def test_a_txn_with_a_primary_source_passes(registered: Ledger) -> None:
    txn_id = add_txn(registered, "whole")
    add_source(registered, txn_id, "manual:transactions.csv:2")

    check(registered.conn)


def test_removing_the_primary_source_of_a_txn_fails_at_commit(
    registered: Ledger,
) -> None:
    txn_id = add_txn(registered, "whole")
    add_source(registered, txn_id, "manual:transactions.csv:2")
    check(registered.conn)  # the txn passes its own check before its source goes
    registered.conn.execute(delete(txn_source).where(txn_source.c.txn_id == txn_id))

    with pytest.raises(IntegrityError, match="has no primary source"):
        check(registered.conn)


def test_removing_the_txn_with_its_sources_is_fine(registered: Ledger) -> None:
    txn_id = add_txn(registered, "whole")
    add_source(registered, txn_id, "manual:transactions.csv:2")
    registered.conn.execute(delete(txn_source))
    registered.conn.execute(delete(txn))

    check(registered.conn)


def test_a_txn_has_one_primary_source(registered: Ledger) -> None:
    txn_id = add_txn(registered, "whole")
    add_source(registered, txn_id, "manual:transactions.csv:2")

    with pytest.raises(IntegrityError), registered.conn.begin_nested():
        add_source(registered, txn_id, "manual:transactions.csv:3")


def test_a_line_key_belongs_to_one_txn(registered: Ledger) -> None:
    first = add_txn(registered, "first")
    second = add_txn(registered, "second")
    add_source(registered, first, "manual:transactions.csv:2", key="shared")
    add_source(registered, second, "manual:transactions.csv:3")
    add_source(
        registered,
        second,
        "manual:transactions.csv:4",
        key="shared",
        role="additional",
    )

    with pytest.raises(IntegrityError, match="names more than one txn"):
        check(registered.conn)


def test_sources_of_one_txn_may_share_a_line_key(registered: Ledger) -> None:
    # A CSV and a PDF that print a purchase alike give two sources one key.
    txn_id = add_txn(registered, "whole")
    add_source(registered, txn_id, "manual:transactions.csv:2", key="shared")
    add_source(
        registered,
        txn_id,
        "manual:transactions.csv:3",
        key="shared",
        role="additional",
    )

    check(registered.conn)


def test_a_source_is_a_document_line_or_a_manual_reference_never_both(
    registered: Ledger,
) -> None:
    txn_id = add_txn(registered, "whole")

    with pytest.raises(IntegrityError), registered.conn.begin_nested():
        registered.conn.execute(
            insert(txn_source).values(
                txn_id=txn_id, line_key_id=new_id("none"), role="primary"
            )
        )


def test_a_tag_of_business_needs_a_basis_class(registered: Ledger) -> None:
    txn_id = add_txn(registered, "tagged", tag="personal")
    add_source(registered, txn_id, "manual:transactions.csv:2")
    with pytest.raises(IntegrityError), registered.conn.begin_nested():
        add_txn(registered, "business", tag="business")
    with pytest.raises(IntegrityError), registered.conn.begin_nested():
        add_txn(registered, "split", tag="split", tag_basis_class=None)

    ok = add_txn(registered, "business ok", tag="business", tag_basis_class="owner")
    add_source(registered, ok, "manual:transactions.csv:3")
    check(registered.conn)


def test_a_business_unit_is_any_text_the_owner_settings_name(
    registered: Ledger,
) -> None:
    # The unit list is the owner's settings, outside the repo; the column takes the
    # free text a decision carries.
    txn_id = add_txn(registered, "unit", business_unit="studio")
    add_source(registered, txn_id, "manual:transactions.csv:2")
    check(registered.conn)

    assert registered.conn.scalar(select(txn.c.business_unit)) == "studio"


def test_the_tax_year_follows_the_recognition_date(registered: Ledger) -> None:
    txn_id = add_txn(registered, "dated", recognition_date=dt.date(2025, 12, 31))
    add_source(registered, txn_id, "manual:transactions.csv:2")
    undated = add_txn(registered, "undated")
    add_source(registered, undated, "manual:transactions.csv:3")

    years = dict(registered.conn.execute(select(txn.c.id, txn.c.tax_year)).all())

    assert years[txn_id] == 2025
    assert years[undated] is None
