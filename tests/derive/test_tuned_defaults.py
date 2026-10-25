"""The shipped pairing defaults (settings.py, ADR-0012), tuned at the Phase 3b gate.

The other derive tests pin the design spec's worked examples (``support.RULE_SETTINGS``:
a 5.00 CAD floor, a 7-day window, a 5 percent spread) so they read as the spec does.
These drop that pin and derive with the defaults themselves: no absolute floor (the
1.5 percent alone), a 5-day transfer window, a 6 percent conversion spread. Seams as
in the rule tests: archive files in, ``derive.run``, the ``pair_edge`` rows and fee
lines out. Every date and amount is invented.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
from decimal import Decimal

import pytest

from rheo_finance.settings import parse_settings

from .support import CSV_IDENTIFIER, FIXTURES, Ledger
from .test_conversions import PAYPAL, edges, fee_lines, usd_rate
from .test_resolution import register
from .test_transfer_edges import (
    BILL_PAYMENT,
    PAYMENT_RECEIVED,
    file_bank,
    file_card,
    on,
    own,
)
from .test_transfer_edges import edges as transfer_edges

D = dt.date
# ``own`` is a shared fixture: re-exported so pytest finds it here.
__all__ = ["own"]


@pytest.fixture
def ledger(ledger: Ledger) -> Ledger:
    """The derive ledger with the shipped pairing defaults: only its paths set."""
    root = ledger.archive.parent
    ledger.ctx = dataclasses.replace(
        ledger.ctx,
        settings=parse_settings(
            f'[finance.paths]\narchive = "{ledger.archive}"\n'
            f'downloads = "{root / "downloads-unset"}"\n'
        ),
    )
    return ledger


@pytest.mark.parametrize(
    ("paid", "received", "edge"),
    [
        ("20.00", "19.70", True),
        ("20.00", "19.69", False),
        ("20.00", "15.00", False),
        ("4.00", "4.50", False),
    ],
    ids=["inside-1.5pct", "just-outside", "inside-a-5-floor", "small-lines"],
)
def test_the_default_tolerance_is_the_percent_alone(
    own: Ledger, paid: str, received: str, edge: bool
) -> None:
    # 1.5 percent of 20.00 is 0.30. Two small lines of different amounts are two
    # movements, not one with a fee, so no CAD floor joins them.
    sent, arrived = D(2025, 6, 2), D(2025, 6, 4)
    file_bank(own, *on(CSV_IDENTIFIER, (sent, BILL_PAYMENT, -Decimal(paid))))
    file_card(own, (arrived, PAYMENT_RECEIVED, Decimal(received)))

    own.derive()

    expected = [(sent, -Decimal(paid), arrived, Decimal(received))]
    assert transfer_edges(own) == (expected if edge else [])


@pytest.mark.parametrize(("days", "edge"), [(5, True), (6, False)])
def test_the_default_transfer_window_is_five_days(
    own: Ledger, days: int, edge: bool
) -> None:
    sent = D(2025, 6, 2)
    arrived = sent + dt.timedelta(days=days)
    file_bank(own, *on(CSV_IDENTIFIER, (sent, BILL_PAYMENT, Decimal("-200.00"))))
    file_card(own, (arrived, PAYMENT_RECEIVED, Decimal("200.00")))

    own.derive()

    expected = [(sent, Decimal("-200.00"), arrived, Decimal("200.00"))]
    assert transfer_edges(own) == (expected if edge else [])


@pytest.mark.parametrize(
    ("rate", "fee"),
    [
        ("1.3263", "-2.20"),  # 37.80 CAD in: a 5.5 percent spread pairs
        ("1.3150", None),  # 37.48 CAD in: over 6 percent, no pair
    ],
    ids=["5.5pct", "over-6pct"],
)
def test_the_default_conversion_spread_tolerance_is_six_percent(
    ledger: Ledger, rate: str, fee: str | None
) -> None:
    # The fixture converts 40.00 CAD to 28.50 USD on one day.
    day = D(2026, 4, 3)
    register(ledger, PAYPAL)
    usd_rate(ledger, rate, on=day)
    ledger.file(
        "paypal/activity-2026-04-20.csv",
        (FIXTURES / "paypal_activity.csv").read_text(encoding="utf-8"),
    )

    ledger.derive()

    assert [applied for _, _, applied, _ in edges(ledger)] == (
        [True] if fee is not None else []
    )
    expected = [] if fee is None else [("derived_fee", Decimal(fee), "CAD")]
    assert fee_lines(ledger, day) == expected
