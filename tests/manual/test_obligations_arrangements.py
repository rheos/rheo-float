"""``obligations.csv`` and ``arrangements.csv`` through the public stage 2 loaders
(FR-10, FR-19; AC-10): a bill row without ``issued_on`` and an arrangement row without
``agreed_on`` are rejected with their line numbers while every other row loads; a
manual debt without ``issued_on`` loads with no lower bound; each instalment's issue
date is its ``agreed_on``. Every payee, date and amount is invented.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from pathlib import Path

from sqlalchemy import Connection, select

from rheo_finance.context import UnitOfWork
from rheo_finance.manual.accounts import ACCOUNTS_FILE, IDENTIFIERS_FILE
from rheo_finance.manual.arrangements import (
    ARRANGEMENTS_FILE,
    ArrangementRow,
    Arrangements,
)
from rheo_finance.manual.headers import MANUAL_FILES
from rheo_finance.manual.obligations import (
    OBLIGATIONS_FILE,
    ManualObligations,
    ObligationRow,
)
from rheo_finance.manual.payees import PAYEES_FILE
from rheo_finance.manual.stage2 import (
    load_accounts,
    load_arrangements,
    load_obligations,
    load_payees,
)
from rheo_finance.storage.schema import manual_entry_error

D = dt.date


def write(data: Path, name: str, rows: list[str]) -> None:
    manual = data / "manual"
    manual.mkdir(exist_ok=True)
    header = ",".join(MANUAL_FILES[name])
    (manual / name).write_text("\n".join([header, *rows]) + "\n", encoding="utf-8")


def payees(conn: Connection, data: Path) -> UnitOfWork:
    """One account, and two payees from ``payees.csv``."""
    write(
        data,
        ACCOUNTS_FILE,
        ["chequing,Chequing,Sample Bank,chequing,CAD,monthly,,,,,,,,,"],
    )
    write(data, IDENTIFIERS_FILE, ["chequing,999000000091,number,,"])
    write(
        data,
        PAYEES_FILE,
        [
            "sample-isp,Sample Internet,vendor,SAMPLE ISP,,",
            "friend,A friend,person,SAMPLE FRIEND,,",
        ],
    )
    return UnitOfWork(conn)


def obligations(conn: Connection, data: Path, rows: list[str]) -> ManualObligations:
    uow = payees(conn, data)
    write(data, OBLIGATIONS_FILE, rows)
    return load_obligations(uow, data, load_payees(uow, data, load_accounts(uow, data)))


def arrangements(conn: Connection, data: Path, rows: list[str]) -> Arrangements:
    uow = payees(conn, data)
    write(data, ARRANGEMENTS_FILE, rows)
    return load_arrangements(
        uow, data, load_payees(uow, data, load_accounts(uow, data))
    )


def errors(conn: Connection, name: str) -> dict[int, str]:
    rows = conn.execute(
        select(manual_entry_error.c.line_no, manual_entry_error.c.reason).where(
            manual_entry_error.c.file == name
        )
    )
    return {int(line): str(reason) for line, reason in rows}


def test_the_row_models_have_the_files_columns() -> None:
    assert tuple(ObligationRow.model_fields) == MANUAL_FILES[OBLIGATIONS_FILE]
    assert tuple(ArrangementRow.model_fields) == MANUAL_FILES[ARRANGEMENTS_FILE]


# --- obligations.csv ------------------------------------------------------------------


def test_a_bill_row_without_issued_on_is_rejected_and_the_others_load(
    owner_conn: Connection, tmp_path: Path
) -> None:
    found = obligations(
        owner_conn,
        tmp_path,
        [
            "sample-isp,bill,,SP-0041,80.00,,2026-02-15,_mail/sample-isp-1.eml,2026-01-30",
            "sample-isp,bill,,SP-0042,80.00,,2026-03-15,,",
            "sample-isp,bill,bill-2026-04,SP-0043,82.50,,2026-04-15,,2026-03-30",
        ],
    )

    reasons = errors(owner_conn, OBLIGATIONS_FILE)
    assert list(reasons) == [3]
    assert "issued_on: a bill row needs the bill date as printed" in reasons[3]
    assert [(each.line_no, each.row.period) for each in found.obligations] == [
        (2, "bill-2026-02"), (4, "bill-2026-04"),
    ]  # fmt: skip
    first = found.obligations[0]
    assert (first.row.issued_on, first.row.amount_due, first.row.archive_path) == (
        D(2026, 1, 30), Decimal("80.00"), "_mail/sample-isp-1.eml",
    )  # fmt: skip
    assert first.source_ref == "manual:obligations.csv:2"


def test_a_manual_debt_without_issued_on_loads_with_no_lower_bound(
    owner_conn: Connection, tmp_path: Path
) -> None:
    found = obligations(
        owner_conn,
        tmp_path,
        [
            "friend,manual_debt,loan-back,,300.00,,,,",
            "friend,manual_debt,debt-tools,,45.00,,2026-05-01,,2026-04-01",
        ],
    )

    assert errors(owner_conn, OBLIGATIONS_FILE) == {}
    rows = [(each.row.period, each.row.issued_on, each.row.due_date) for each in
            found.obligations]  # fmt: skip
    assert rows == [
        ("debt-loan-back", None, None),
        ("debt-tools", D(2026, 4, 1), D(2026, 5, 1)),
    ]


def test_bad_obligation_rows_are_reported_by_line_and_the_rest_load(
    owner_conn: Connection, tmp_path: Path
) -> None:
    found = obligations(
        owner_conn,
        tmp_path,
        [
            "sample-isp,bill,,SP-0041,80.00,,2026-02-15,,2026-01-30",  # 2: loads
            "nobody,bill,,SP-0042,80.00,,2026-03-15,,2026-02-28",  # 3: no payee
            "sample-isp,bill,,SP-0043,80.00,,,,2026-03-30",  # 4: no due date
            "sample-isp,bill,bill-2026-07,,80.00,,2026-06-15,,2026-05-30",  # 5: month
            "sample-isp,bill,,,80.005,,2026-08-15,,2026-07-30",  # 6: sub-cent
            "sample-isp,bill,,,80.00,90.00,2026-09-15,,2026-08-30",  # 7: minimum
            "friend,manual_debt,,,10.00,,,,",  # 8: no debt slug
            "sample-isp,arrears,,,10.00,,2026-10-15,,2026-09-30",  # 9: kind
            "friend,manual_debt,twice,,10.00,,,,",  # 10: key repeated
            "friend,manual_debt,debt-twice,,12.00,,,,",  # 11: key repeated
            "sample-isp,bill,,,80.00,,2026-11-15,/abs/x.eml,2026-10-30",  # 12: path
            "friend,manual_debt,huge,,1e30,,,,",  # 13: too large, the load goes on
        ],
    )

    reasons = errors(owner_conn, OBLIGATIONS_FILE)
    assert sorted(reasons) == [3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13]
    assert reasons[3].startswith("payee:")
    assert "due_date:" in reasons[4]
    assert "period_label: a bill's period" in reasons[5]
    assert reasons[6].startswith("amount_due:")
    assert "minimum:" in reasons[7]
    assert "period_label: a manual_debt row" in reasons[8]
    assert reasons[9].startswith("kind:")
    assert reasons[10].startswith("period_label: another row")
    assert reasons[12].startswith("archive_path:")
    assert reasons[13].startswith("amount_due:")
    assert [each.line_no for each in found.obligations] == [2]


# --- arrangements.csv -----------------------------------------------------------------


def test_an_arrangement_row_without_agreed_on_is_rejected_and_the_others_load(
    owner_conn: Connection, tmp_path: Path
) -> None:
    found = arrangements(
        owner_conn,
        tmp_path,
        [
            "plan-a,sample-isp,2026-01-10,1,50.00,2026-02-01,bill-2025-11;bill-2025-12",
            "plan-a,sample-isp,,2,50.00,2026-03-01,",
            "plan-a,sample-isp,2026-01-10,3,50.00,2026-04-01,bill-2025-12",
        ],
    )

    reasons = errors(owner_conn, ARRANGEMENTS_FILE)
    assert list(reasons) == [3]
    assert reasons[3].startswith("agreed_on:")
    (plan,) = found.arrangements
    assert (plan.slug, plan.payee, plan.agreed_on, plan.replaces_periods) == (
        "plan-a", "sample-isp", D(2026, 1, 10), ("bill-2025-11", "bill-2025-12"),
    )  # fmt: skip
    instalments = [
        (each.line_no, each.row.period, each.row.issued_on, each.source_ref)
        for each in plan.instalments
    ]
    assert instalments == [
        (2, "inst-plan-a-1", D(2026, 1, 10), "manual:arrangements.csv:2"),
        (4, "inst-plan-a-3", D(2026, 1, 10), "manual:arrangements.csv:4"),
    ]


def test_bad_arrangement_rows_are_reported_by_line_and_the_rest_load(
    owner_conn: Connection, tmp_path: Path
) -> None:
    found = arrangements(
        owner_conn,
        tmp_path,
        [
            "plan-a,sample-isp,2026-01-10,1,50.00,2026-02-01,",  # 2: loads
            "plan-b,nobody,2026-01-10,1,50.00,2026-02-01,",  # 3: no payee
            "plan-a,sample-isp,2026-01-10,1,60.00,2026-03-01,",  # 2 and 4: repeated
            "plan-c,sample-isp,2026-01-10,1,0.00,2026-02-01,",  # 5: zero
            "plan-d,sample-isp,2026-01-10,1,10.00,2026-02-01,old-bill-2025-11",  # 6
            "plan-e,sample-isp,2026-01-10,1,10.00,2026-02-01,",  # 7 and 8: disagree
            "plan-e,friend,2026-01-10,2,10.00,2026-03-01,",
            "plan-f,sample-isp,2026-01-10,0,10.00,2026-02-01,",  # 9: instalment 0
            "plan-g,friend,2026-02-01,1,10.00,2026-03-01,",  # 10: loads
        ],
    )

    reasons = errors(owner_conn, ARRANGEMENTS_FILE)
    assert sorted(reasons) == [2, 3, 4, 5, 6, 7, 8, 9]
    assert reasons[2].startswith("instalment: another row")
    assert reasons[3].startswith("payee:")
    assert "amount:" in reasons[5]
    assert reasons[6].startswith("replaces_periods")
    assert reasons[7].startswith("payee: the rows of this arrangement disagree")
    assert reasons[9].startswith("instalment:")
    assert [plan.slug for plan in found.arrangements] == ["plan-g"]
