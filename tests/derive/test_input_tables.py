"""Every input table a derive reads feeds a part of the input fingerprint (carried W1,
SPEC "Derivation"), so new input state makes the next derive run again.

Seams: ``derive.fingerprint.fingerprint`` over the derive ``ledger``; the table groups
of ``derive.run``. The rate is invented.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

from sqlalchemy import insert

from rheo_finance.derive import fingerprint as fp
from rheo_finance.derive import run as derive_run
from rheo_finance.storage.schema import rate, rate_coverage

from .support import NOW, Ledger


def moved_parts(before: fp.Fingerprint, after: fp.Fingerprint) -> set[str]:
    return {name for name in before.parts if before.parts[name] != after.parts[name]}


def test_every_input_table_feeds_a_fingerprint_part(ledger: Ledger) -> None:
    # Every table is cleared, rewritten, state or file-owned (test_derive_run); the
    # last two are what a derive reads and does not rebuild.
    inputs = set(derive_run.STATE) | set(derive_run.FILE_OWNED)
    assert set(fp.STATE_PARTS) == inputs
    parts = fp.fingerprint(ledger.uow, ledger.ctx).parts
    assert set(fp.STATE_PARTS.values()) <= set(parts)


def test_a_stored_rate_moves_the_rates_part_and_nothing_else(ledger: Ledger) -> None:
    before = fp.fingerprint(ledger.uow, ledger.ctx)
    ledger.conn.execute(
        insert(rate).values(
            rate_date=dt.date(2025, 12, 10),
            base="USD",
            quote="CAD",
            rate=Decimal("1.3725"),
            source="boc",
            fetched_at=NOW,
        )
    )
    after = fp.fingerprint(ledger.uow, ledger.ctx)

    assert moved_parts(before, after) == {"rates"}
    assert after.digest != before.digest


def test_a_coverage_row_alone_moves_the_rates_part(ledger: Ledger) -> None:
    # A fetch that returns no new observation still records the range it covered,
    # and that alone lets stage 4 convert a weekend or holiday line: the next derive
    # must run again.
    before = fp.fingerprint(ledger.uow, ledger.ctx)
    ledger.conn.execute(
        insert(rate_coverage).values(
            series="FXUSDCAD",
            source="boc",
            covered_from=dt.date(2025, 12, 3),
            covered_to=dt.date(2025, 12, 10),
            fetched_at=NOW,
        )
    )
    after = fp.fingerprint(ledger.uow, ledger.ctx)

    assert moved_parts(before, after) == {"rates"}
    assert after.digest != before.digest
