"""Cash lines, document periods and filed years (FR-9, FR-10, FR-13, AC-10 bad rows).

Every test goes through the public stage 2 loaders (``load_cash_lines``,
``load_document_periods``, ``load_filed_years``) with CSV files written to a temporary
data directory, and reads ``manual_entry_error`` and ``filed_year`` back inside the
test's rolled-back transaction. Every value is synthetic.
"""

from __future__ import annotations

import datetime as dt
import uuid
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import BaseModel
from sqlalchemy import Connection, insert, select

from rheo_finance.context import UnitOfWork
from rheo_finance.manual.accounts import ACCOUNTS_FILE, IDENTIFIERS_FILE
from rheo_finance.manual.cash_lines import TRANSACTIONS_FILE, CashLineRow
from rheo_finance.manual.document_periods import (
    DOCUMENT_PERIODS_FILE,
    DocumentPeriodRow,
)
from rheo_finance.manual.filed_years import FILED_YEARS_FILE, FiledYearRow
from rheo_finance.manual.headers import MANUAL_FILES
from rheo_finance.manual.stage2 import (
    load_accounts,
    load_cash_lines,
    load_document_periods,
    load_filed_years,
)
from rheo_finance.storage.schema import filed_year, manual_entry_error


def write(data: Path, name: str, rows: list[str]) -> None:
    manual = data / "manual"
    manual.mkdir(exist_ok=True)
    header = ",".join(MANUAL_FILES[name])
    (manual / name).write_text("\n".join([header, *rows]) + "\n", encoding="utf-8")


def errors(conn: Connection) -> list[tuple[str, int, str]]:
    rows = conn.execute(
        select(
            manual_entry_error.c.file,
            manual_entry_error.c.line_no,
            manual_entry_error.c.reason,
        )
    )
    return sorted((str(f), int(n), str(r)) for f, n, r in rows)


def error_lines(conn: Connection, name: str) -> list[int]:
    return sorted({line for file, line, _ in errors(conn) if file == name})


def stored_years(conn: Connection) -> list[tuple[int, dt.date | None, str | None]]:
    rows = conn.execute(
        select(filed_year.c.year, filed_year.c.filed_on, filed_year.c.note)
    )
    return sorted((int(y), f, n) for y, f, n in rows)


@pytest.mark.parametrize(
    ("name", "model"),
    [
        (TRANSACTIONS_FILE, CashLineRow),
        (DOCUMENT_PERIODS_FILE, DocumentPeriodRow),
        (FILED_YEARS_FILE, FiledYearRow),
    ],
)
def test_each_row_model_has_its_files_columns(
    name: str, model: type[BaseModel]
) -> None:
    assert tuple(model.model_fields) == MANUAL_FILES[name]


# --- header-only files ----------------------------------------------------------------


def test_header_only_files_load_as_empty(
    owner_conn: Connection, tmp_path: Path
) -> None:
    for name in (TRANSACTIONS_FILE, DOCUMENT_PERIODS_FILE, FILED_YEARS_FILE):
        write(tmp_path, name, [])
    uow = UnitOfWork(owner_conn)
    registry = load_accounts(uow, tmp_path)

    cash = load_cash_lines(uow, tmp_path, registry)
    periods = load_document_periods(uow, tmp_path)
    filed = load_filed_years(uow, tmp_path)

    assert (cash.lines, cash.errors) == ((), ())
    assert (periods.periods, periods.errors) == ((), ())
    assert (filed.years, filed.errors) == ((), ())
    assert errors(owner_conn) == []
    assert stored_years(owner_conn) == []


# --- transactions.csv -----------------------------------------------------------------


def test_a_cash_line_with_no_account_goes_to_paid_outside(
    owner_conn: Connection, tmp_path: Path
) -> None:
    write(
        tmp_path,
        ACCOUNTS_FILE,
        ["test-wallet,Test wallet,Test Bank,wallet,CAD,none,,,,,,,,,"],
    )
    write(tmp_path, IDENTIFIERS_FILE, ["test-wallet,9991,number,,"])
    write(
        tmp_path,
        TRANSACTIONS_FILE,
        [
            ",2024-03-05,-12.50,cad,Test market,",
            "test-wallet,2024-03-06,40.005,USD,Test refund,a synthetic note",
        ],
    )
    uow = UnitOfWork(owner_conn)
    registry = load_accounts(uow, tmp_path)

    cash = load_cash_lines(uow, tmp_path, registry)

    assert cash.errors == ()
    first, second = cash.lines
    assert first.row.account == "paid-outside"
    assert (first.row.date, first.row.amount, first.row.currency) == (
        dt.date(2024, 3, 5),
        Decimal("-12.50"),
        "CAD",
    )
    assert first.source_ref == "manual:transactions.csv:2"
    assert second.row.account == "test-wallet"
    assert second.row.amount == Decimal("40.005")
    assert second.row.note == "a synthetic note"
    assert second.source_ref == "manual:transactions.csv:3"


def test_a_bad_cash_line_is_reported_by_line_and_the_rest_load(
    owner_conn: Connection, tmp_path: Path
) -> None:
    write(
        tmp_path,
        TRANSACTIONS_FILE,
        [
            ",2024-03-05,-12.50,CAD,Test market,",  # 2: good
            ",2024-02-30,-1.00,CAD,Test market,",  # 3: no such date
            ",2024-03-05,0,CAD,Test market,",  # 4: zero amount
            ",2024-03-05,ten,CAD,Test market,",  # 5: not a number
            "no-such-account,2024-03-05,-1.00,CAD,Test market,",  # 6
            ",2024-03-05,-1.00,CAD,,",  # 7: no description
            ",2024-03-07,7.25,CAD,Test sale,",  # 8: good
        ],
    )
    uow = UnitOfWork(owner_conn)
    registry = load_accounts(uow, tmp_path)

    cash = load_cash_lines(uow, tmp_path, registry)

    assert [line.line_no for line in cash.lines] == [2, 8]
    assert error_lines(owner_conn, TRANSACTIONS_FILE) == [3, 4, 5, 6, 7]
    assert sorted({e.line_no for e in cash.errors}) == [3, 4, 5, 6, 7]
    reasons = {line: reason for file, line, reason in errors(owner_conn)}
    assert reasons[4] == "Value error, amount is zero"
    assert reasons[6] == "account: no loaded account has this slug"
    assert "description" in reasons[7]


def test_each_loader_rewrites_only_its_own_files_errors(
    owner_conn: Connection, tmp_path: Path
) -> None:
    uow = UnitOfWork(owner_conn)
    registry = load_accounts(uow, tmp_path)  # no files: the built-in account alone
    owner_conn.execute(
        insert(manual_entry_error),
        [
            {"id": uuid.uuid4(), "file": name, "line_no": 9, "reason": "stale"}
            for name in (
                ACCOUNTS_FILE,
                TRANSACTIONS_FILE,
                DOCUMENT_PERIODS_FILE,
                FILED_YEARS_FILE,
            )
        ],
    )
    write(tmp_path, TRANSACTIONS_FILE, [",2024-03-05,0,CAD,Test market,"])
    write(tmp_path, DOCUMENT_PERIODS_FILE, ["test/a.pdf,2024-02-01,2024-01-01,"])
    write(tmp_path, FILED_YEARS_FILE, ["not-a-year,,"])

    load_cash_lines(uow, tmp_path, registry)
    load_document_periods(uow, tmp_path)
    load_filed_years(uow, tmp_path)

    assert [(f, n) for f, n, _ in errors(owner_conn)] == [
        (ACCOUNTS_FILE, 9),
        (DOCUMENT_PERIODS_FILE, 2),
        (FILED_YEARS_FILE, 2),
        (TRANSACTIONS_FILE, 2),
    ]


# --- document_periods.csv -------------------------------------------------------------


def test_document_periods_load_and_a_bad_row_is_reported_by_line(
    owner_conn: Connection, tmp_path: Path
) -> None:
    write(
        tmp_path,
        DOCUMENT_PERIODS_FILE,
        [
            "test-issuer/2024-01 bill.jpg,2024-01-01,2024-01-31,typed in",  # 2: good
            "test-issuer/b.pdf,2024-02-29,2024-02-01,",  # 3: end before start
            "/abs/c.pdf,2024-01-01,2024-01-31,",  # 4: absolute
            "test-issuer/../d.pdf,2024-01-01,2024-01-31,",  # 5: dot-dot part
            "test-issuer/e.pdf,2024-01-01,2024-01-31,",  # 6: same path as 7
            "test-issuer/e.pdf,2024-02-01,2024-02-29,",  # 7
            "test-issuer/f.pdf,2024-03-01,,",  # 8: no end
            "test-issuer/g.pdf,2024-04-01,2024-04-30,",  # 9: good
        ],
    )

    periods = load_document_periods(UnitOfWork(owner_conn), tmp_path)

    assert [p.archive_path for p in periods.periods] == [
        "test-issuer/2024-01 bill.jpg",
        "test-issuer/g.pdf",
    ]
    first = periods.periods[0]
    assert (first.period_start, first.period_end, first.note) == (
        dt.date(2024, 1, 1),
        dt.date(2024, 1, 31),
        "typed in",
    )
    assert error_lines(owner_conn, DOCUMENT_PERIODS_FILE) == [3, 4, 5, 6, 7, 8]


# --- filed_years.csv ------------------------------------------------------------------


def test_filed_years_are_stored_and_a_bad_row_is_reported_by_line(
    owner_conn: Connection, tmp_path: Path
) -> None:
    write(
        tmp_path,
        FILED_YEARS_FILE,
        [
            "2019,2020-04-30,a synthetic note",  # 2: good
            "twenty,,",  # 3: not a year
            "2020,2020-12-31,",  # 4: filed before the year ended
            "2021,,",  # 5: same year as 6
            "2021,2022-04-01,",  # 6
            "1899,,",  # 7: out of range
            "2022,,",  # 8: good
        ],
    )

    filed = load_filed_years(UnitOfWork(owner_conn), tmp_path)

    assert [row.year for row in filed.years] == [2019, 2022]
    assert stored_years(owner_conn) == [
        (2019, dt.date(2020, 4, 30), "a synthetic note"),
        (2022, None, None),
    ]
    assert error_lines(owner_conn, FILED_YEARS_FILE) == [3, 4, 5, 6, 7]


def test_filed_years_are_rewritten_on_every_load(
    owner_conn: Connection, tmp_path: Path
) -> None:
    uow = UnitOfWork(owner_conn)
    write(tmp_path, FILED_YEARS_FILE, ["2019,,", "2020,,"])
    load_filed_years(uow, tmp_path)
    id_2020 = select(filed_year.c.id).where(filed_year.c.year == 2020)
    first_id = owner_conn.scalar(id_2020)

    write(tmp_path, FILED_YEARS_FILE, ["2020,,"])
    load_filed_years(uow, tmp_path)

    assert stored_years(owner_conn) == [(2020, None, None)]
    assert owner_conn.scalar(id_2020) == first_id  # a rebuild keeps the id

    write(tmp_path, FILED_YEARS_FILE, [])
    load_filed_years(uow, tmp_path)
    assert stored_years(owner_conn) == []
