"""Same-account currency conversions: the ``conversion`` edge, its spread and its one
fee line (FR-15(c), Derivation stage 9 ``conversion``, edge case 69, AC-85).

Seam: ``derive.run`` through the derive ``ledger`` over Wise and PayPal documents read
by the real parsers (the ``wise_history.txt``, ``wise_statement.csv`` and
``paypal_activity.csv`` fixtures, and Wise statement and PayPal activity CSVs built
here in the same layouts), with Bank of Canada rates stored as a fetch leaves them;
read back as the ``conversion`` rows of ``pair_edge``, the legs' transfer states, the
fee lines on the conversion date and the CAD totals. Every date, amount and rate is
invented.

The parsers print a conversion's out-leg net of the issuer's fee, with the fee as its
own line, so the spread is the out-leg's CAD value minus the in-leg's: the printed fee
is never taken off it a second time.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest
from sqlalchemy import ColumnElement, select

from rheo_finance.ledger.totals import CadTotal, cad_total
from rheo_finance.manual.stage2 import account_id
from rheo_finance.manual.transfer_hints import TRANSFER_HINTS_FILE
from rheo_finance.storage.schema import pair_edge, txn

from .support import FIXTURES, Ledger
from .test_conversion import cover, store_rates
from .test_resolution import Account, register, table

D = dt.date
WISE_ID = "P99900001"
PAYPAL_ID = "SAMPLE999A9ID"
WISE: Account = ("wise", "Wise", "wallet", "CAD;USD;EUR", WISE_ID)
PAYPAL: Account = ("paypal", "PayPal", "payment_processor", "CAD;USD", PAYPAL_ID)
CONVERSION = "General Currency Conversion"
DAY = D(2025, 7, 8)

# One Wise statement row: (id, day, amount with fee, currency, description, from, to,
# fee, to amount).
WiseRow = tuple[str, dt.date, str, str, str, str, str, str, str]
# One PayPal activity row: (day, type, currency, gross), or with its printed fee
# (negative) after the gross.
PaypalRow = tuple[dt.date, str, str, str] | tuple[dt.date, str, str, str, str]
Leg = tuple[dt.date, Decimal, str, str | None, str | None]


def wise_csv(*rows: WiseRow) -> str:
    """A Wise statement CSV of ``rows``, given oldest first and written newest first
    as the export is, each currency's balance running from 1,000.00."""
    balances: dict[str, Decimal] = {}
    found = []
    for ident, day, amount, code, description, source, target, fee, to in rows:
        balances[code] = balances.get(code, Decimal("1000.00")) + Decimal(amount)
        found.append(
            {
                "TransferWise ID": ident, "Date": day.strftime("%d-%m-%Y"),
                "Amount": amount, "Currency": code, "Description": description,
                "Running Balance": f"{balances[code]:.2f}", "Exchange From": source,
                "Exchange To": target, "Total fees": fee, "Exchange To Amount": to,
            }
        )  # fmt: skip
    return table("wise_statement.csv", found[::-1])


def converted(
    ident: str, day: dt.date, out: str, out_code: str, into: str, into_code: str,
    fee: str = "0.00",
) -> tuple[WiseRow, WiseRow]:  # fmt: skip
    """A Wise conversion: the out row (its amount the converted ``out`` plus ``fee``)
    and the in row, as the export prints a ``BALANCE`` transaction."""
    words = f"Converted {out} {out_code} to {into} {into_code}"
    paid = f"-{Decimal(out) + Decimal(fee):.2f}"
    return (
        (ident, day, paid, out_code, words, out_code, into_code, fee, into),
        (ident, day, into, into_code, words, out_code, into_code, "0.00", into),
    )


def paypal_csv(*rows: PaypalRow) -> str:
    """A PayPal activity CSV of ``rows``, oldest first, a fee only where a row gives
    one, each currency's balance running from 500.00."""
    balances: dict[str, Decimal] = {}
    found = []
    for n, row in enumerate(rows):
        day, kind, code, gross = row[:4]
        fee = row[4] if len(row) == 5 else "0.00"
        net = f"{Decimal(gross) + Decimal(fee):.2f}"
        balances[code] = balances.get(code, Decimal("500.00")) + Decimal(net)
        found.append(
            {
                "Date": day.strftime("%d/%m/%Y"), "Time": f"08:00:{n:02d}",
                "TimeZone": "PDT", "Type": kind, "Status": "Completed",
                "Currency": code, "Gross": gross, "Fee": fee, "Net": net,
                "Balance": f"{balances[code]:.2f}",
                "Balance Impact": "Credit" if not gross.startswith("-") else "Debit",
            }
        )  # fmt: skip
    return table("paypal_activity.csv", found)


def usd_rate(ledger: Ledger, value: str, *, on: dt.date = DAY) -> None:
    store_rates(ledger, "USD", (on, value))
    cover(ledger, "FXUSDCAD", (on, on + dt.timedelta(days=30)))


def legs(ledger: Ledger) -> list[Leg]:
    """Every line with conversion wording (the parsers' fee lines carry none)."""
    rows = ledger.conn.execute(
        select(txn.c.posting_date, txn.c.amount, txn.c.currency,
               txn.c.transfer_state, txn.c.pair_kind)
        .where(txn.c.transfer_like)
    )  # fmt: skip
    return sorted(tuple(row) for row in rows)


def edges(ledger: Ledger) -> list[tuple[Decimal, Decimal, bool, Decimal | None]]:
    """The conversion edges as (out amount, in amount, applied, spread)."""
    a, b = txn.alias("a"), txn.alias("b")
    rows = ledger.conn.execute(
        select(a.c.amount, b.c.amount, pair_edge.c.applied, pair_edge.c.spread_cad)
        .join(a, a.c.id == pair_edge.c.a_txn_id)
        .join(b, b.c.id == pair_edge.c.b_txn_id)
        .where(pair_edge.c.kind == "conversion")
    )
    return sorted(tuple(row) for row in rows)


def fee_lines(ledger: Ledger, day: dt.date) -> list[tuple[str, Decimal, str]]:
    """The fee lines posted on ``day`` as (origin, amount, currency)."""
    rows = ledger.conn.execute(
        select(txn.c.origin, txn.c.amount, txn.c.currency).where(
            txn.c.fee_type.is_not(None), txn.c.posting_date == day
        )
    )
    return sorted(tuple(row) for row in rows)


def counted(ledger: Ledger, *filters: ColumnElement[bool]) -> CadTotal:
    """The lines that count (no transfer state) ``filters`` select, in CAD."""
    return cad_total(ledger.uow, [txn.c.transfer_state.is_(None), *filters])


def paired(day: dt.date, amount: str, code: str) -> Leg:
    return (day, Decimal(amount), code, "paired", "conversion")


def left(day: dt.date, amount: str, code: str, state: str) -> Leg:
    return (day, Decimal(amount), code, state, None)


# --- Wise: one fee line, printed or derived -------------------------------------------


@pytest.mark.parametrize(
    ("fee", "expected"),
    [("0.80", ("document_line", "-0.80", "USD")),
     ("0.00", ("derived_fee", "-3.00", "CAD"))],
    ids=["printed", "derived"],
)  # fmt: skip
def test_a_conversion_date_carries_exactly_one_fee_line_printed_or_derived(
    ledger: Ledger, fee: str, expected: tuple[str, str, str]
) -> None:
    # 300.00 USD at 1.3600 is 408.00 CAD against 405.00 CAD in: a spread of 3.00,
    # inside 5 percent of 408.00. Printed, the fee is the parser's own line and the
    # spread is booked nowhere; not printed, the spread is the one fee line.
    register(ledger, WISE)
    usd_rate(ledger, "1.3600")
    ledger.file(
        "wise/statement-2025-07-31.csv",
        wise_csv(*converted("BALANCE-999000201", DAY, "300.00", "USD", "405.00",
                            "CAD", fee)),
    )  # fmt: skip

    ledger.derive()

    assert legs(ledger) == [
        paired(DAY, "-300.00", "USD"),
        paired(DAY, "405.00", "CAD"),
    ]
    # The spread is the out-leg's CAD value minus the in-leg's, the printed fee not
    # taken off it a second time.
    assert edges(ledger) == [
        (Decimal("-300.00"), Decimal("405.00"), True, Decimal("3.00"))
    ]
    origin, amount, code = expected
    assert fee_lines(ledger, DAY) == [(origin, Decimal(amount), code)]
    # The legs count zero times; the fee once, as spending; nothing is income.
    spent = counted(ledger, txn.c.amount < 0)
    assert spent.sum == (Decimal("-3.00") if fee == "0.00" else Decimal("-1.09"))
    assert counted(ledger, txn.c.amount > 0).sum == Decimal("0.00")


@pytest.mark.parametrize(
    ("name", "path"),
    [("wise_history.txt", "wise/transactions-2026-02-01_to_2026-02-28.pdf"),
     ("wise_statement.csv", "wise/statement-2026-02-28.csv")],
    ids=["history-pdf", "statement-csv"],
)  # fmt: skip
def test_the_wise_fixtures_pair_their_conversion_and_keep_the_printed_fee_alone(
    ledger: Ledger, name: str, path: str
) -> None:
    # The fixtures convert 100.00 USD (a printed fee of 0.80 USD) to 135.20 CAD. At
    # 1.3600 the out-leg is worth 136.00 CAD: a spread of 0.80, booked nowhere.
    day = D(2026, 2, 20)
    register(ledger, WISE)
    usd_rate(ledger, "1.3600", on=day)
    ledger.file(path, (FIXTURES / name).read_text(encoding="utf-8"))

    ledger.derive()

    assert paired(day, "-100.00", "USD") in legs(ledger)
    assert paired(day, "135.20", "CAD") in legs(ledger)
    assert edges(ledger) == [
        (Decimal("-100.00"), Decimal("135.20"), True, Decimal("0.80"))
    ]
    assert fee_lines(ledger, day) == [("document_line", Decimal("-0.80"), "USD")]
    named = ledger.conn.scalar(select(pair_edge.c.fee_txn_id))
    printed = ledger.conn.scalar(
        select(txn.c.id).where(txn.c.fee_type.is_not(None), txn.c.posting_date == day)
    )
    assert named == printed


# --- PayPal: the spread tolerance in both directions ----------------------------------


@pytest.mark.parametrize(
    ("rate", "pairs", "fee"),
    [
        ("1.3544", True, "-1.40"),  # 38.60 CAD in: a 3.5 percent spread
        ("1.3333", True, "-2.00"),  # 38.00 CAD in: exactly 5 percent
        ("1.4100", True, None),  # 40.18 CAD in: richer than the out-leg
        ("1.3330", False, None),  # 37.99 CAD in: just over 5 percent
        ("1.2800", False, None),  # 36.48 CAD in: far under
        ("1.5000", False, None),  # 42.75 CAD in: far over
    ],
    ids=["spread-3.5pct", "at-5pct", "in-leg-richer", "just-outside", "low",
         "high"],
)  # fmt: skip
def test_the_paypal_conversion_pairs_only_inside_its_own_spread_tolerance(
    ledger: Ledger, rate: str, pairs: bool, fee: str | None
) -> None:
    # The fixture converts 40.00 CAD to 28.50 USD on one day. The transfer tolerance
    # (the larger of 5.00 CAD and 1.5 percent) is never used here.
    day = D(2026, 4, 3)
    register(ledger, PAYPAL)
    usd_rate(ledger, rate, on=day)
    ledger.file(
        "paypal/activity-2026-04-20.csv",
        (FIXTURES / "paypal_activity.csv").read_text(encoding="utf-8"),
    )

    ledger.derive()

    out, into = ("-40.00", "CAD"), ("28.50", "USD")
    found = legs(ledger)
    if pairs:
        assert {paired(day, *out), paired(day, *into)} <= set(found)
        assert [applied for _, _, applied, _ in edges(ledger)] == [True]
    else:
        # No edge, so nothing measured them: both legs are unresolved.
        assert {left(day, *out, "unresolved"), left(day, *into, "unresolved")} <= set(
            found
        )
        assert edges(ledger) == []
    expected = [] if fee is None else [("derived_fee", Decimal(fee), "CAD")]
    assert fee_lines(ledger, day) == expected
    # A richer in-leg is a zero fee, never income.
    income = counted(ledger, txn.c.amount > 0, txn.c.posting_date == day)
    assert income.sum == Decimal("0.00")


def test_a_paypal_spread_of_three_and_a_half_percent_pairs_on_a_built_export(
    ledger: Ledger,
) -> None:
    # 200.00 CAD to 154.40 USD at 1.2500: 193.00 CAD in, a spread of 7.00. The
    # transfer tolerance (5.00 CAD here) would refuse it; the spread tolerance holds.
    register(ledger, PAYPAL)
    usd_rate(ledger, "1.2500")
    ledger.file(
        "paypal/activity-2025-07-31.csv",
        paypal_csv((DAY, CONVERSION, "CAD", "-200.00"),
                   (DAY, CONVERSION, "USD", "154.40")),
    )  # fmt: skip

    ledger.derive()

    assert legs(ledger) == [
        paired(DAY, "-200.00", "CAD"),
        paired(DAY, "154.40", "USD"),
    ]
    assert edges(ledger) == [
        (Decimal("-200.00"), Decimal("154.40"), True, Decimal("7.00"))
    ]
    assert fee_lines(ledger, DAY) == [("derived_fee", Decimal("-7.00"), "CAD")]
    fee_account = ledger.conn.scalar(
        select(txn.c.account_id).where(txn.c.origin == "derived_fee")
    )
    assert fee_account == account_id("paypal")


# --- a printed fee line belongs to at most one conversion -----------------------------


def fee_owners(ledger: Ledger) -> list[tuple[Decimal, Decimal | None]]:
    """Each conversion edge's out-leg amount and the amount of the fee line it names."""
    out, fee = txn.alias("out"), txn.alias("fee")
    rows = ledger.conn.execute(
        select(out.c.amount, fee.c.amount)
        .select_from(pair_edge)
        .join(out, out.c.id == pair_edge.c.a_txn_id)
        .outerjoin(fee, fee.c.id == pair_edge.c.fee_txn_id)
        .where(pair_edge.c.kind == "conversion")
    )
    return sorted((row[0], row[1]) for row in rows)


def test_two_conversions_with_their_own_wording_each_keep_their_printed_fee(
    ledger: Ledger,
) -> None:
    # The statement's wording names each conversion's amounts, so each fee line is
    # one conversion's.
    register(ledger, WISE)
    usd_rate(ledger, "1.3600")
    ledger.file(
        "wise/statement-2025-07-31.csv",
        wise_csv(
            *converted("BALANCE-999000208", DAY, "300.00", "USD", "405.00", "CAD",
                       "0.80"),
            *converted("BALANCE-999000209", DAY, "100.00", "USD", "135.00", "CAD",
                       "0.40"),
        ),
    )  # fmt: skip

    ledger.derive()

    assert fee_owners(ledger) == [
        (Decimal("-300.00"), Decimal("-0.80")),
        (Decimal("-100.00"), Decimal("-0.40")),
    ]
    assert fee_lines(ledger, DAY) == [
        ("document_line", Decimal("-0.80"), "USD"),
        ("document_line", Decimal("-0.40"), "USD"),
    ]


@pytest.mark.parametrize("swapped", [False, True], ids=["in-order", "swapped"])
@pytest.mark.parametrize("second_fee", ["-0.50", None], ids=["both", "one"])
def test_same_wording_conversions_on_one_day_leave_the_printed_fees_no_ones(
    ledger: Ledger, second_fee: str | None, swapped: bool
) -> None:
    # Two conversions print the same wording on one day, so a fee line cannot be told
    # to be either one's: no edge names one, and neither books a derived fee (each
    # has a positive spread, 7.00 and 3.50), since a printed fee is the only fee line
    # of its date. Whatever the order of the rows, the result is the same.
    register(ledger, PAYPAL)
    usd_rate(ledger, "1.2500")
    second: PaypalRow = (
        (DAY, CONVERSION, "CAD", "-100.00")
        if second_fee is None
        else (DAY, CONVERSION, "CAD", "-100.00", second_fee)
    )
    first: list[PaypalRow] = [
        (DAY, CONVERSION, "CAD", "-200.00", "-1.00"),
        (DAY, CONVERSION, "USD", "154.40"),
    ]
    later: list[PaypalRow] = [second, (DAY, CONVERSION, "USD", "77.20")]
    rows = later + first if swapped else first + later
    ledger.file("paypal/activity-2025-07-31.csv", paypal_csv(*rows))  # fmt: skip

    ledger.derive()

    assert fee_owners(ledger) == [
        (Decimal("-200.00"), None),
        (Decimal("-100.00"), None),
    ]
    assert [applied for _, _, applied, _ in edges(ledger)] == [True, True]
    printed = [("document_line", Decimal("-1.00"), "CAD")]
    if second_fee is not None:
        printed.insert(0, ("document_line", Decimal(second_fee), "CAD"))
    assert fee_lines(ledger, DAY) == sorted(printed)


# --- ambiguity, the window and other accounts -----------------------------------------


def test_two_candidate_in_legs_leave_every_leg_ambiguous(ledger: Ledger) -> None:
    register(ledger, WISE)
    usd_rate(ledger, "1.3600")
    out, first = converted(
        "BALANCE-999000202", DAY, "300.00", "USD", "405.00", "CAD", "0.80"
    )
    # A second in-leg the next day, from another conversion whose out-leg is not here.
    _, second = converted(
        "BALANCE-999000203", DAY + dt.timedelta(days=1), "300.00", "USD", "405.00",
        "CAD",
    )  # fmt: skip
    ledger.file("wise/statement-2025-07-31.csv", wise_csv(out, first, second))

    ledger.derive()

    assert legs(ledger) == [
        left(DAY, "-300.00", "USD", "ambiguous"),
        left(DAY, "405.00", "CAD", "ambiguous"),
        left(DAY + dt.timedelta(days=1), "405.00", "CAD", "ambiguous"),
    ]
    assert [applied for _, _, applied, _ in edges(ledger)] == [False, False]
    # Both candidates are the one out-leg's, so both name its printed fee line.
    assert fee_owners(ledger) == [(Decimal("-300.00"), Decimal("-0.80"))] * 2
    assert fee_lines(ledger, DAY) == [("document_line", Decimal("-0.80"), "USD")]


@pytest.mark.parametrize(("gap", "edge"), [(1, True), (2, False)], ids=["1", "2"])
def test_the_legs_post_at_most_the_conversion_window_apart(
    ledger: Ledger, gap: int, edge: bool
) -> None:
    register(ledger, PAYPAL)
    usd_rate(ledger, "1.2500")
    ledger.file(
        "paypal/activity-2025-07-31.csv",
        paypal_csv((DAY, CONVERSION, "CAD", "-200.00"),
                   (DAY + dt.timedelta(days=gap), CONVERSION, "USD", "154.40")),
    )  # fmt: skip

    ledger.derive()

    assert len(edges(ledger)) == int(edge)


def test_conversion_legs_on_two_accounts_are_never_a_conversion(
    ledger: Ledger,
) -> None:
    # A CAD out-leg on Wise and a USD in-leg on PayPal: no conversion edge. The
    # transfer rule handles them (here they net inside its tolerance).
    register(ledger, WISE, PAYPAL)
    usd_rate(ledger, "1.2500")
    out, _ = converted("BALANCE-999000204", DAY, "200.00", "CAD", "158.00", "USD")
    ledger.file("wise/statement-2025-07-31.csv", wise_csv(out))
    ledger.file(
        "paypal/activity-2025-07-31.csv",
        paypal_csv((DAY, CONVERSION, "USD", "158.00")),
    )

    ledger.derive()

    assert edges(ledger) == []
    kinds = ledger.conn.scalars(select(pair_edge.c.kind)).all()
    assert kinds == ["own_transfer"]
    assert fee_lines(ledger, DAY) == [("derived_fee", Decimal("-2.50"), "CAD")]


def test_two_legs_in_one_currency_are_never_a_conversion(ledger: Ledger) -> None:
    register(ledger, PAYPAL)
    ledger.file(
        "paypal/activity-2025-07-31.csv",
        paypal_csv((DAY, CONVERSION, "CAD", "-40.00"),
                   (DAY, CONVERSION, "CAD", "40.00")),
    )  # fmt: skip

    ledger.derive()

    assert edges(ledger) == []
    assert legs(ledger) == [
        left(DAY, "-40.00", "CAD", "unresolved"),
        left(DAY, "40.00", "CAD", "unresolved"),
    ]


def test_a_printed_fee_is_never_a_leg_even_when_a_conversion_row_matches_it(
    ledger: Ledger,
) -> None:
    # The owner's ``conversion`` row matches the fee line's wording too. The fee
    # (0.80 USD, 1.09 CAD) would sit inside the spread tolerance of a lone 1.10 CAD
    # in-leg the same day; it stays a fee line that counts once, and the lone in-leg
    # is unresolved.
    register(ledger, WISE)
    ledger.manual(TRANSFER_HINTS_FILE, "converted,conversion,")
    usd_rate(ledger, "1.3600")
    pair = converted("BALANCE-999000206", DAY, "300.00", "USD", "405.00", "CAD", "0.80")
    _, lone = converted("BALANCE-999000207", DAY, "0.80", "USD", "1.10", "CAD")
    ledger.file("wise/statement-2025-07-31.csv", wise_csv(*pair, lone))

    ledger.derive()

    assert legs(ledger) == [
        paired(DAY, "-300.00", "USD"),
        left(DAY, "1.10", "CAD", "unresolved"),
        paired(DAY, "405.00", "CAD"),
    ]
    assert fee_lines(ledger, DAY) == [("document_line", Decimal("-0.80"), "USD")]
    assert counted(ledger, txn.c.amount < 0).sum == Decimal("-1.09")


def test_a_printed_conversion_fee_is_never_paired_away_by_a_small_transfer_like_line(
    ledger: Ledger,
) -> None:
    # A Wise conversion prints a fee of 0.80 USD (1.09 CAD at 1.3600). The same day a
    # PayPal bank deposit of 1.00 CAD, transfer-like by its wording, sits inside the
    # transfer tolerance of that fee. The fee line is no end of a transfer: it stays
    # the date's one fee line and counts once, and the deposit is unresolved.
    register(ledger, WISE, PAYPAL)
    usd_rate(ledger, "1.3600")
    ledger.file(
        "wise/statement-2025-07-31.csv",
        wise_csv(*converted("BALANCE-999000210", DAY, "300.00", "USD", "405.00",
                            "CAD", "0.80")),
    )  # fmt: skip
    ledger.file(
        "paypal/activity-2025-07-31.csv",
        paypal_csv((DAY, "Bank Deposit to PP Account", "CAD", "1.00")),
    )

    ledger.derive()

    assert fee_lines(ledger, DAY) == [("document_line", Decimal("-0.80"), "USD")]
    assert ledger.conn.scalars(select(pair_edge.c.kind)).all() == ["conversion"]
    assert left(DAY, "1.00", "CAD", "unresolved") in legs(ledger)
    assert counted(ledger, txn.c.amount < 0).sum == Decimal("-1.09")


# --- no stored rate -------------------------------------------------------------------


def test_a_conversion_with_no_stored_rate_pairs_with_no_fee_until_a_rate_exists(
    ledger: Ledger,
) -> None:
    # USD to EUR, neither currency with a rate: the wording settles the pair, both
    # legs sit in the unconverted count and no fee line is made. Once the rates are
    # stored, the next derive measures the spread and books it.
    register(ledger, WISE)
    ledger.file(
        "wise/statement-2025-07-31.csv",
        wise_csv(*converted("BALANCE-999000205", DAY, "300.00", "USD", "270.00",
                            "EUR")),
    )  # fmt: skip

    ledger.derive()

    assert legs(ledger) == [
        paired(DAY, "-300.00", "USD"),
        paired(DAY, "270.00", "EUR"),
    ]
    assert edges(ledger) == [(Decimal("-300.00"), Decimal("270.00"), True, None)]
    assert fee_lines(ledger, DAY) == []
    assert cad_total(ledger.uow, [txn.c.transfer_like]).unconverted_count == 2

    usd_rate(ledger, "1.3600")
    store_rates(ledger, "EUR", (DAY, "1.4900"))
    cover(ledger, "FXEURCAD", (DAY, DAY + dt.timedelta(days=30)))
    ledger.derive()

    # 408.00 CAD out, 402.30 CAD in: a spread of 5.70.
    assert edges(ledger) == [
        (Decimal("-300.00"), Decimal("270.00"), True, Decimal("5.70"))
    ]
    assert fee_lines(ledger, DAY) == [("derived_fee", Decimal("-5.70"), "CAD")]
    assert cad_total(ledger.uow, [txn.c.transfer_like]).unconverted_count == 0
