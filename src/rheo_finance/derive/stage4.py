"""Derivation stage 4: conversion to CAD (FR-14, AC-15).

Every txn gets ``cad_amount``, ``cad_source``, ``rate``, ``rate_date`` and
``conversion_state``:

- A CAD txn is ``native``: its amount is its CAD amount, never converted, even when its
  line also states a foreign original (a card purchase made in USD, billed in CAD).
- A fiat txn in another currency whose primary line carries an issuer-stated CAD amount
  (``document_line.stated_cad``) uses that amount, source ``issuer``, ``converted``;
  the rate stored is the one it implies, on the line's date. It is never re-converted.
- Any other fiat txn uses the stored Bank of Canada rate of the last business day on or
  before its date (``rates.RateBook.on_or_before``: only across days a fetch covered;
  the date is the transaction date, else the posting date), source ``boc``,
  ``converted``. With no usable rate it is ``unconverted``, with no CAD amount, and
  stays out of every CAD total (``ledger.totals.cad_total`` counts it).
- A crypto txn is ``unconverted`` until the crypto price sources land.

Each converted amount rounds half-even to the cent on its own line; totals are sums of
rounded lines.
"""

from __future__ import annotations

import datetime as dt
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Final

from sqlalchemy import and_, bindparam, func, select, update

from rheo_finance.context import UnitOfWork
from rheo_finance.rates import QUOTE_CAD, SOURCE_BOC, RateBook, rate_book
from rheo_finance.storage.schema import document_line, txn, txn_source

CENT: Final = Decimal("0.01")
# The digits kept of a rate an issuer's stated amount implies.
IMPLIED_RATE: Final = Decimal("1e-10")


def cents(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_EVEN)


def _converted(
    row_id: object,
    cad: Decimal | None,
    source: str | None,
    used_rate: Decimal | None,
    rate_date: dt.date | None,
    state: str,
) -> dict[str, object]:
    return {
        "b_id": row_id,
        "b_cad": cad,
        "b_source": source,
        "b_rate": used_rate,
        "b_rate_date": rate_date,
        "b_state": state,
    }


def convert(uow: UnitOfWork) -> None:
    """Set the conversion columns of every txn."""
    rows = uow.execute(
        select(
            txn.c.id,
            txn.c.amount,
            txn.c.currency,
            txn.c.movement_kind,
            func.coalesce(txn.c.txn_date, txn.c.posting_date).label("day"),
            document_line.c.stated_cad,
        )
        .select_from(txn)
        .outerjoin(
            txn_source,
            and_(txn_source.c.txn_id == txn.c.id, txn_source.c.role == "primary"),
        )
        .outerjoin(document_line, document_line.c.id == txn_source.c.document_line_id)
    ).all()
    books: dict[str, RateBook] = {}
    values: list[dict[str, object]] = []
    for row in rows:
        amount: Decimal = row.amount
        if row.currency == QUOTE_CAD:
            values.append(
                _converted(row.id, cents(amount), "native", None, None, "native")
            )
        elif row.movement_kind != "fiat":
            values.append(_converted(row.id, None, None, None, None, "unconverted"))
        elif row.stated_cad is not None:
            stated: Decimal = row.stated_cad
            implied = (
                abs(stated / amount).quantize(IMPLIED_RATE) if amount != 0 else None
            )
            values.append(
                _converted(
                    row.id, cents(stated), "issuer", implied, row.day, "converted"
                )
            )
        else:
            if row.currency not in books:
                books[row.currency] = rate_book(uow, row.currency)
            found = books[row.currency].on_or_before(row.day)
            if found is None:
                values.append(_converted(row.id, None, None, None, None, "unconverted"))
            else:
                day, used = found
                values.append(
                    _converted(
                        row.id, cents(amount * used), SOURCE_BOC, used, day, "converted"
                    )
                )
    if values:
        uow.execute(
            update(txn)
            .where(txn.c.id == bindparam("b_id"))
            .values(
                cad_amount=bindparam("b_cad"),
                cad_source=bindparam("b_source"),
                rate=bindparam("b_rate"),
                rate_date=bindparam("b_rate_date"),
                conversion_state=bindparam("b_state"),
            ),
            values,
        )
