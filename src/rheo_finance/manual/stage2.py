"""Derivation stage 2, the manual load, from the files under ``<data>/manual/``.

Each loader rewrites the ``manual_entry_error`` rows of its own files and no others
(another file's errors are its own loader's to rewrite):

- ``load_accounts``: ``accounts.csv`` and ``account_identifiers.csv`` through the
  registry rules (``manual.accounts``) into ``account`` and ``account_identifier``;
- ``load_cash_lines``: ``transactions.csv``, checked against the loaded registry and
  held for stage 3 (``manual.cash_lines``);
- ``load_document_periods``: ``document_periods.csv``, checked against the loaded
  registry and held for stage 6 (``manual.document_periods``);
- ``load_filed_years``: ``filed_years.csv`` into ``filed_year``
  (``manual.filed_years``);
- ``load_transfer_hints``: ``transfer_hints.csv``, checked against the loaded
  registry and held in memory for ``reference.patterns.classify`` and stage 9
  (``manual.transfer_hints``); its patterns are restricted and never stored.

Ids come from ``derived_id`` on the fixed business date 2000-01-01 (registry rows have
no date of their own). An account is keyed by its slug. An identifier is keyed by its
account's slug and its place among that account's identifiers (sorted by kind and
text), never by the identifier itself: an id is public in references, and a hash of a
short account number can be reversed by trying every number.
"""

from __future__ import annotations

import datetime as dt
import uuid
from pathlib import Path
from typing import Final

from sqlalchemy import delete, insert

from rheo_finance.context import UnitOfWork
from rheo_finance.manual.accounts import (
    ACCOUNTS_FILE,
    IDENTIFIERS_FILE,
    AccountIdentifierRow,
    AccountRow,
    Registry,
    build_registry,
)
from rheo_finance.manual.cash_lines import (
    TRANSACTIONS_FILE,
    CashLineRow,
    CashLines,
    build_cash_lines,
)
from rheo_finance.manual.document_periods import (
    DOCUMENT_PERIODS_FILE,
    DocumentPeriodRow,
    DocumentPeriods,
    build_document_periods,
)
from rheo_finance.manual.filed_years import (
    FILED_YEARS_FILE,
    FiledYearRow,
    FiledYears,
    build_filed_years,
)
from rheo_finance.manual.headers import MANUAL_DIR_NAME
from rheo_finance.manual.loader import RowError, load_csv
from rheo_finance.manual.transfer_hints import (
    TRANSFER_HINTS_FILE,
    TransferHintRow,
    TransferHints,
    build_transfer_hints,
)
from rheo_finance.storage.ids import derived_id
from rheo_finance.storage.schema import (
    account,
    account_identifier,
    filed_year,
    manual_entry_error,
)

REGISTRY_DATE: Final = dt.date(2000, 1, 1)


def account_id(slug: str) -> uuid.UUID:
    return derived_id(REGISTRY_DATE, ("account", slug))


def error_id(error: RowError) -> uuid.UUID:
    return derived_id(
        REGISTRY_DATE,
        ("manual_entry_error", error.file, error.line_no, error.reason),
    )


def _account_values(row: AccountRow) -> dict[str, object]:
    return {
        "id": account_id(row.slug),
        "slug": row.slug,
        "label": row.label,
        "institution": row.institution,
        "kind": row.kind,
        "currencies": list(row.currencies),
        "cadence": row.cadence,
        "owner": row.owner,
        "opened_on": row.opened,
        "closed_on": row.closed,
        "sourced_by": row.sourced_by,
        "placeholder": row.placeholder,
        "missing_before": row.missing_before,
        "missing_reason": row.missing_reason,
    }


def _identifier_values(
    identifiers: tuple[AccountIdentifierRow, ...],
) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    seen: dict[str, int] = {}
    for ident in sorted(identifiers, key=lambda i: (i.account, i.kind, i.identifier)):
        place = seen.get(ident.account, 0)
        seen[ident.account] = place + 1
        rows.append(
            {
                "id": derived_id(
                    REGISTRY_DATE, ("account_identifier", ident.account, place)
                ),
                "account_id": account_id(ident.account),
                "identifier": ident.identifier,
                "kind": ident.kind,
                "valid_from": ident.valid_from,
                "valid_to": ident.valid_to,
            }
        )
    return rows


def load_accounts(uow: UnitOfWork, data_dir: Path) -> Registry:
    """Rewrite the registry tables from the manual files under ``data_dir``."""
    manual = data_dir / MANUAL_DIR_NAME
    registry = build_registry(
        load_csv(manual / ACCOUNTS_FILE, AccountRow),
        load_csv(manual / IDENTIFIERS_FILE, AccountIdentifierRow),
    )
    uow.execute(delete(account_identifier))
    uow.execute(delete(account))
    uow.execute(insert(account), [_account_values(row) for row in registry.accounts])
    uow.execute(insert(account_identifier), _identifier_values(registry.identifiers))
    _rewrite_errors(uow, (ACCOUNTS_FILE, IDENTIFIERS_FILE), registry.errors)
    return registry


def load_cash_lines(uow: UnitOfWork, data_dir: Path, registry: Registry) -> CashLines:
    """The ``transactions.csv`` lines whose account is in ``registry``, for stage 3."""
    cash = build_cash_lines(
        load_csv(data_dir / MANUAL_DIR_NAME / TRANSACTIONS_FILE, CashLineRow),
        {row.slug for row in registry.accounts},
    )
    _rewrite_errors(uow, (TRANSACTIONS_FILE,), cash.errors)
    return cash


def load_document_periods(
    uow: UnitOfWork, data_dir: Path, registry: Registry
) -> DocumentPeriods:
    """The ``document_periods.csv`` periods whose account is in ``registry``, for
    stage 6."""
    periods = build_document_periods(
        load_csv(data_dir / MANUAL_DIR_NAME / DOCUMENT_PERIODS_FILE, DocumentPeriodRow),
        {row.slug for row in registry.accounts},
    )
    _rewrite_errors(uow, (DOCUMENT_PERIODS_FILE,), periods.errors)
    return periods


def load_transfer_hints(
    uow: UnitOfWork, data_dir: Path, registry: Registry
) -> TransferHints:
    """The ``transfer_hints.csv`` rows whose counterpart (if any) is in ``registry``,
    for ``reference.patterns.classify`` and stage 9."""
    hints = build_transfer_hints(
        load_csv(data_dir / MANUAL_DIR_NAME / TRANSFER_HINTS_FILE, TransferHintRow),
        {row.slug for row in registry.accounts},
    )
    _rewrite_errors(uow, (TRANSFER_HINTS_FILE,), hints.errors)
    return hints


def filed_year_id(year: int) -> uuid.UUID:
    # The registry date, not the year: derived_id's time field starts in 1970.
    return derived_id(REGISTRY_DATE, ("filed_year", year))


def load_filed_years(uow: UnitOfWork, data_dir: Path) -> FiledYears:
    """Rewrite ``filed_year`` from ``filed_years.csv``."""
    filed = build_filed_years(
        load_csv(data_dir / MANUAL_DIR_NAME / FILED_YEARS_FILE, FiledYearRow)
    )
    uow.execute(delete(filed_year))
    if filed.years:
        uow.execute(
            insert(filed_year),
            [
                {
                    "id": filed_year_id(row.year),
                    "year": row.year,
                    "filed_on": row.filed_on,
                    "note": row.note,
                }
                for row in filed.years
            ],
        )
    _rewrite_errors(uow, (FILED_YEARS_FILE,), filed.errors)
    return filed


def _rewrite_errors(
    uow: UnitOfWork, files: tuple[str, ...], errors: tuple[RowError, ...]
) -> None:
    """Replace the ``manual_entry_error`` rows of ``files``, and only theirs."""
    uow.execute(delete(manual_entry_error).where(manual_entry_error.c.file.in_(files)))
    if errors:
        uow.execute(
            insert(manual_entry_error),
            [
                {
                    "id": error_id(error),
                    "file": error.file,
                    "line_no": error.line_no,
                    "reason": error.reason,
                }
                for error in errors
            ],
        )
