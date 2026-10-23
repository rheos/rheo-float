"""The status, coverage and document report views, as SQLAlchemy selects that
migration 0014 compiles (FR-48, FR-51, SPEC Data Models > Report views).

Each view shows only codes, counts, dates, labels and registered paths: never an
account identifier, a description, a merchant key, a counterparty or the path of a
skipped file (a skipped file never reaches ``document``, and ``archive_skip`` holds
counts by top-level directory).

``report_status`` holds the facts ``finance.status.get`` builds its lines from, one
row per fact as ``(fact, subject, count, at)``:

- ``schema``: the revision the database is at, as ``subject``;
- ``last_import``: per top-level archive directory, how many documents it holds and
  the newest ``imported_at`` (a nested issuer's sub-directory is free text, so it is
  folded into its top-level directory);
- ``document_status``: documents per status;
- ``source_discrepancy``: discrepancies per kind;
- ``archive_skip_identifier``: per directory, the files the walk skipped because their
  path carries an identifier (only that reason: the other skip reasons are not fixed
  by a rename);
- ``account_no_data``: one row per account with no coverage month, by label, leaving
  out the built-in "Paid outside recorded accounts", which has no source by design;
- ``missing_months``: per account with any, its ``missing`` coverage months, by label.

``report_coverage`` is the matrix: account slug and label, month, state and the reason
as words (``running_balance_break`` reads ``running balance break``).

``report_document`` is the registry: path, kind, status, period, official and
extraction. A withheld document shows ``(withheld)`` for its path.

Migration 0014 holds what these selects compiled to as frozen SQL; the frozen-views
guard compares them.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable
from typing import Final

from sqlalchemy import (
    BigInteger,
    DateTime,
    Select,
    Text,
    cast,
    column,
    func,
    literal,
    null,
    select,
    table,
    union_all,
)
from sqlalchemy.sql.elements import Label

from rheo_finance.derive.coverage import State
from rheo_finance.documents.registry import SkipReason
from rheo_finance.manual.accounts import INTERNAL_ACCOUNT
from rheo_finance.storage.schema import (
    account,
    archive_skip,
    coverage,
    document,
    source_discrepancy,
)

# The fact names of ``report_status``.
SCHEMA: Final = "schema"
LAST_IMPORT: Final = "last_import"
DOCUMENT_STATUS: Final = "document_status"
SOURCE_DISCREPANCY: Final = "source_discrepancy"
ARCHIVE_SKIP_IDENTIFIER: Final = "archive_skip_identifier"
ACCOUNT_NO_DATA: Final = "account_no_data"
MISSING_MONTHS: Final = "missing_months"

IDENTIFIER_IN_PATH: Final[SkipReason] = "identifier_in_path"
MISSING: Final[State] = "missing"

# Alembic's version table: not in the shared MetaData, which mirrors ledger tables.
_version = table("alembic_version_finance", column("version_num"), schema="finance")


def _fact(name: str) -> Label[str]:
    return literal(name, Text).label("fact")


def _one() -> Label[int]:
    return cast(literal(1), BigInteger).label("count")


def _no_time() -> Label[dt.datetime]:
    return cast(null(), DateTime(timezone=True)).label("at")


def report_status_select() -> Select[tuple[object, ...]]:
    top = func.split_part(document.c.issuer, "/", 1)
    with_data = select(coverage.c.account_id).where(
        coverage.c.account_id == account.c.id
    )
    branches = [
        select(
            _fact(SCHEMA), _version.c.version_num.label("subject"), _one(), _no_time()
        ),
        select(
            _fact(LAST_IMPORT),
            top.label("subject"),
            func.count().label("count"),
            func.max(document.c.imported_at).label("at"),
        ).group_by(top),
        select(
            _fact(DOCUMENT_STATUS),
            document.c.status.label("subject"),
            func.count().label("count"),
            _no_time(),
        ).group_by(document.c.status),
        select(
            _fact(SOURCE_DISCREPANCY),
            source_discrepancy.c.kind.label("subject"),
            func.count().label("count"),
            _no_time(),
        ).group_by(source_discrepancy.c.kind),
        select(
            _fact(ARCHIVE_SKIP_IDENTIFIER),
            archive_skip.c.directory.label("subject"),
            cast(archive_skip.c.count, BigInteger).label("count"),
            _no_time(),
        ).where(archive_skip.c.reason == IDENTIFIER_IN_PATH),
        select(
            _fact(ACCOUNT_NO_DATA),
            account.c.label.label("subject"),
            _one(),
            _no_time(),
        ).where(~with_data.exists(), account.c.slug != INTERNAL_ACCOUNT.slug),
        select(
            _fact(MISSING_MONTHS),
            account.c.label.label("subject"),
            func.count().label("count"),
            _no_time(),
        )
        .select_from(coverage.join(account, coverage.c.account_id == account.c.id))
        .where(coverage.c.state == MISSING)
        .group_by(account.c.id, account.c.label),
    ]
    # A view's body is one select; the union is wrapped as its only source.
    unioned = union_all(*branches).subquery("facts")
    return select(unioned.c.fact, unioned.c.subject, unioned.c.count, unioned.c.at)


def report_coverage_select() -> Select[tuple[object, ...]]:
    return select(
        account.c.slug.label("account"),
        account.c.label.label("account_label"),
        coverage.c.month,
        coverage.c.state,
        func.replace(coverage.c.reason_code, "_", " ").label("reason"),
    ).select_from(coverage.join(account, coverage.c.account_id == account.c.id))


def report_document_select() -> Select[tuple[object, ...]]:
    return select(
        document.c.path,
        document.c.kind,
        document.c.status,
        document.c.period_start,
        document.c.period_end,
        document.c.official,
        document.c.extraction,
    )


# Each view this module defines, by name, for the frozen-SQL guard test.
VIEW_SELECTS: Final[dict[str, Callable[[], Select[tuple[object, ...]]]]] = {
    "report_status": report_status_select,
    "report_coverage": report_coverage_select,
    "report_document": report_document_select,
}
