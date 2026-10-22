"""Derivation stage 2, the manual load: the account registry part.

``load_accounts`` reads ``accounts.csv`` and ``account_identifiers.csv`` from
``<data>/manual/``, applies the registry rules (``manual.accounts``) and rewrites the
derived tables ``account`` and ``account_identifier``, and the ``manual_entry_error``
rows of those two files (another file's errors are its own loader's to rewrite).

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
from rheo_finance.manual.headers import MANUAL_DIR_NAME
from rheo_finance.manual.loader import RowError, load_csv
from rheo_finance.storage.ids import derived_id
from rheo_finance.storage.schema import account, account_identifier, manual_entry_error

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
    uow.execute(
        delete(manual_entry_error).where(
            manual_entry_error.c.file.in_((ACCOUNTS_FILE, IDENTIFIERS_FILE))
        )
    )
    uow.execute(insert(account), [_account_values(row) for row in registry.accounts])
    uow.execute(insert(account_identifier), _identifier_values(registry.identifiers))
    if registry.errors:
        uow.execute(
            insert(manual_entry_error),
            [
                {
                    "id": error_id(error),
                    "file": error.file,
                    "line_no": error.line_no,
                    "reason": error.reason,
                }
                for error in registry.errors
            ],
        )
    return registry
