"""Derivation stage 2: the manual load.

Runs the loaders of ``manual.stage2`` in the one order that works: the account
registry first (cash lines and document periods are checked against it, and stage 1's
parsers match accounts from it), then the files that read it. Peer-money files join
here when ``features/peer_money`` exists (Key decision 14).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from rheo_finance.context import UnitOfWork
from rheo_finance.manual.accounts import Registry
from rheo_finance.manual.cash_lines import CashLines
from rheo_finance.manual.document_periods import DocumentPeriods
from rheo_finance.manual.filed_years import FiledYears
from rheo_finance.manual.stage2 import (
    load_accounts,
    load_cash_lines,
    load_document_periods,
    load_filed_years,
)


@dataclass(frozen=True)
class ManualLoad:
    """What the manual files held: the registry and the lines and periods stage 3 and
    stage 6 read."""

    registry: Registry
    cash_lines: CashLines
    document_periods: DocumentPeriods
    filed_years: FiledYears


def load_manual(uow: UnitOfWork, data_dir: Path) -> ManualLoad:
    registry = load_accounts(uow, data_dir)
    return ManualLoad(
        registry=registry,
        cash_lines=load_cash_lines(uow, data_dir, registry),
        document_periods=load_document_periods(uow, data_dir, registry),
        filed_years=load_filed_years(uow, data_dir),
    )
