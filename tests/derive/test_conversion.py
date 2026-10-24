"""Derivation stage 4, conversion to CAD, and ``ledger.totals.cad_total`` (FR-14,
AC-15).

Seams: ``derive.run`` over ``FX_LINES`` documents with rates stored in ``rate`` (as a
fetch leaves them), read back from ``txn``; ``cad_total`` over the derived txns. Every
date, amount and rate is invented.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

from sqlalchemy import insert, select

from rheo_finance.ledger.totals import cad_total
from rheo_finance.storage.schema import rate, txn

from .support import NOW, Ledger, fx_lines

D = dt.date


def store_rates(ledger: Ledger, base: str, *rows: tuple[dt.date, str]) -> None:
    ledger.conn.execute(
        insert(rate),
        [
            {
                "rate_date": day,
                "base": base,
                "quote": "CAD",
                "rate": Decimal(value),
                "source": "boc",
                "fetched_at": NOW,
            }
            for day, value in rows
        ],
    )


def converted(ledger: Ledger) -> dict[str, tuple[object, ...]]:
    rows = ledger.conn.execute(
        select(
            txn.c.description_norm,
            txn.c.cad_amount,
            txn.c.cad_source,
            txn.c.rate,
            txn.c.rate_date,
            txn.c.conversion_state,
        )
    )
    return {str(row[0]): tuple(row[1:]) for row in rows}


def test_a_usd_line_converts_at_the_stored_rate_of_its_day_half_even(
    fx_ledger: Ledger,
) -> None:
    store_rates(
        fx_ledger, "USD", (D(2025, 12, 10), "1.3725"), (D(2025, 12, 11), "1.3800")
    )
    fx_ledger.file(
        "synthetic-fx/usd.txt",
        fx_lines("USD", "2025-12-10 -10.00 - - SAMPLE SHOP 99941"),
    )

    fx_ledger.derive()

    # 10.00 x 1.3725 = 13.725: half-even to the even cent.
    assert converted(fx_ledger)["sample shop 99941"] == (
        Decimal("-13.72"),
        "boc",
        Decimal("1.3725"),
        D(2025, 12, 10),
        "converted",
    )


def test_a_weekend_line_takes_the_last_business_day_before_it(
    fx_ledger: Ledger,
) -> None:
    store_rates(
        fx_ledger, "USD", (D(2025, 12, 12), "1.3500"), (D(2025, 12, 15), "1.4000")
    )
    fx_ledger.file(
        "synthetic-fx/usd.txt",
        fx_lines("USD", "2025-12-13 -100.00 - - SAMPLE SHOP 99942"),
    )

    fx_ledger.derive()

    assert converted(fx_ledger)["sample shop 99942"] == (
        Decimal("-135.00"),
        "boc",
        Decimal("1.3500"),
        D(2025, 12, 12),
        "converted",
    )


def test_a_line_with_no_usable_rate_is_unconverted_and_counted_beside_the_total(
    fx_ledger: Ledger,
) -> None:
    store_rates(
        fx_ledger,
        "USD",
        (D(2025, 12, 1), "1.3000"),
        (D(2025, 12, 20), "1.3100"),
        (D(2025, 12, 22), "1.3200"),
    )
    fx_ledger.file(
        "synthetic-fx/usd.txt",
        fx_lines(
            "USD",
            # Nine days after the last rate before it: a hole, not a weekend.
            "2025-12-10 -10.00 - - SAMPLE SHOP 99943",
            # After the newest stored rate: not fetched yet.
            "2025-12-23 -20.00 - - SAMPLE SHOP 99944",
            "2025-12-22 -30.00 - - SAMPLE SHOP 99945",
        ),
    )
    fx_ledger.file(
        "synthetic-fx/eur.txt",
        fx_lines("EUR", "2025-12-22 -40.00 - - SAMPLE SHOP 99946"),
    )

    fx_ledger.derive()

    found = converted(fx_ledger)
    left_out = (None, None, None, None, "unconverted")
    assert found["sample shop 99943"] == left_out
    assert found["sample shop 99944"] == left_out
    assert found["sample shop 99946"] == left_out  # no EUR rate at all
    assert found["sample shop 99945"][0] == Decimal("-39.60")
    total = cad_total(fx_ledger.uow)
    assert total.sum == Decimal("-39.60")
    assert total.unconverted_count == 3
    assert total.text() == "-39.60 CAD (3 unconverted)"
    # Filters narrow both figures.
    usd = cad_total(fx_ledger.uow, [txn.c.currency == "USD"])
    assert (usd.sum, usd.unconverted_count) == (Decimal("-39.60"), 2)


def test_an_issuer_stated_cad_amount_is_never_re_converted(fx_ledger: Ledger) -> None:
    store_rates(fx_ledger, "USD", (D(2025, 12, 10), "1.5000"))
    # A card line billed in CAD for a USD purchase, and a USD line whose issuer
    # states the CAD amount.
    fx_ledger.file(
        "synthetic-fx/cad.txt",
        fx_lines("CAD", "2025-12-10 -13.70 -13.70 USD SAMPLE CARD SHOP 99947"),
    )
    fx_ledger.file(
        "synthetic-fx/usd.txt",
        fx_lines("USD", "2025-12-10 -10.00 -13.70 - SAMPLE STATED SHOP 99948"),
    )

    fx_ledger.derive()

    found = converted(fx_ledger)
    assert found["sample card shop 99947"] == (
        Decimal("-13.70"),
        "native",
        None,
        None,
        "native",
    )
    assert found["sample stated shop 99948"] == (
        Decimal("-13.70"),
        "issuer",
        Decimal("1.3700000000"),
        D(2025, 12, 10),
        "converted",
    )


def test_every_total_prints_its_unconverted_count_even_when_zero(
    fx_ledger: Ledger,
) -> None:
    fx_ledger.file(
        "synthetic-fx/cad.txt",
        fx_lines(
            "CAD",
            "2025-12-10 -12.50 - - SAMPLE SHOP 99949",
            "2025-12-11 40.00 - - SAMPLE REFUND 99950",
        ),
    )

    fx_ledger.derive()

    assert {row[4] for row in converted(fx_ledger).values()} == {"native"}
    assert cad_total(fx_ledger.uow).text() == "27.50 CAD (0 unconverted)"
    assert cad_total(fx_ledger.uow, [txn.c.amount < 0]).text() == (
        "-12.50 CAD (0 unconverted)"
    )
