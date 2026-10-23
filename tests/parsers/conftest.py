"""Shared setup for the parser tests: the account registry a test parses against.

``register_accounts`` writes ``accounts.csv`` and ``account_identifiers.csv`` under a
data directory's ``manual`` folder and loads them (``manual.stage2.load_accounts``),
the way the owner's real registry is loaded. Every value a test passes is synthetic.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from rheo_finance.context import UnitOfWork
from rheo_finance.manual.accounts import ACCOUNTS_FILE, IDENTIFIERS_FILE
from rheo_finance.manual.headers import MANUAL_FILES
from rheo_finance.manual.stage2 import load_accounts


@dataclass(frozen=True)
class ManualAccount:
    """One ``accounts.csv`` row and its ``account_identifiers.csv`` rows, all of
    ``identifier_kind`` and valid from ``valid_from`` to ``valid_to`` (ISO dates,
    empty for open). The label is the slug in title case unless given."""

    slug: str
    institution: str
    kind: str
    identifiers: tuple[str, ...]
    identifier_kind: str = "number"
    currencies: str = "CAD"
    label: str | None = None
    valid_from: str = ""
    valid_to: str = ""


def register_accounts(uow: UnitOfWork, data: Path, *accounts: ManualAccount) -> None:
    """Write the two registry files for ``accounts`` under ``data`` and load them."""
    manual = data / "manual"
    manual.mkdir(parents=True, exist_ok=True)
    columns = MANUAL_FILES[ACCOUNTS_FILE]
    rows = [",".join(columns)]
    identifiers = [",".join(MANUAL_FILES[IDENTIFIERS_FILE])]
    for each in accounts:
        cells = dict.fromkeys(columns, "")
        cells.update(
            slug=each.slug, label=each.label or each.slug.title(),
            institution=each.institution, kind=each.kind,
            currencies=each.currencies, cadence="monthly",
        )  # fmt: skip
        rows.append(",".join(cells[column] for column in columns))
        identifiers += [
            f"{each.slug},{identifier},{each.identifier_kind},"
            f"{each.valid_from},{each.valid_to}"
            for identifier in each.identifiers
        ]
    (manual / ACCOUNTS_FILE).write_text("\n".join(rows) + "\n", encoding="utf-8")
    (manual / IDENTIFIERS_FILE).write_text(
        "\n".join(identifiers) + "\n", encoding="utf-8"
    )
    load_accounts(uow, data)
