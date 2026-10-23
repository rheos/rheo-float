"""The AC-7 parity check: ``parity.compare`` through ``scripts/parity_check.py``'s
output on synthetic pairs (FR-7).

Seams: the script's ``main`` over a ledger built in the test's rolled-back transaction
(``composition.build_context`` swapped for ``rolled_back_context``) from the synthetic
Vancity statement and Virgin bill, against synthetic ``vancity.csv`` and
``virgin-plus.csv`` files written from the ledger's own totals; and ``compare`` with
``report`` over synthetic rows for the kinds the fixtures cannot reach (the one-cent
identity tolerance, a validate-only flag, a ledger-only statement, duplicates). Every
value is synthetic.
"""

from __future__ import annotations

import csv
import datetime as dt
import importlib.util
import re
import socket
from collections.abc import Callable
from decimal import Decimal
from pathlib import Path
from types import ModuleType

import pytest
from sqlalchemy import Connection
from sqlalchemy.engine import URL

from derive.support import FIXTURES, Ledger, MakeContext
from rheo_finance import catalogue, composition, operations, parity
from rheo_finance.contracts import Role
from rheo_finance.derive.ops import StatementListOut
from rheo_finance.operations import dispatch
from rheo_finance.parity import (
    VANCITY_FILE,
    VIRGIN_FILE,
    Kind,
    LedgerStatement,
    ReferenceStatement,
    Result,
    Totals,
    compare,
    ledger_statements,
    read_vancity,
    read_virgin,
    report,
    virgin_verdict,
)
from rheo_finance.parsers.docextract_adapter import money, reference_bill

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "parity_check.py"
STATEMENT = (FIXTURES / "vancity_statement_2026.txt").read_text(encoding="utf-8")
STATEMENT_END = "2026-01-10"
VIRGIN_BILL = (FIXTURES / "virgin_bill.txt").read_text(encoding="utf-8")
VANCITY_HEADER = (
    "period_start,period_end,account,statement_name,opening,withdrawals,deposits,"
    "closing,lowest,fees,flat_fee,per_item_fees,overdraft_fees,overdraft_interest,"
    "nsf_fees,other_fees,overdrawn_from,overdrawn_to,checks_ok"
)
VIRGIN_HEADER = (
    "bill_date,due_date,total_due,unpaid_balance,current_charges,late_payment_charge,"
    "device_payment,adjustments,usage_total,data_used_mb,source,needs_review"
)
ACCOUNTS = (
    ("patronage", "Vancity", "chequing", "999000000011"),
    ("chequing", "Vancity", "chequing", "999000000022"),
    ("savings", "Vancity", "chequing", "999000000033"),
    ("shares", "Vancity", "chequing", "999000000044"),
    ("side-fund", "Vancity", "chequing", "999000000055"),
    ("mobile", "Virgin Plus", "bill_vendor", "999000123"),
)
# Any money shape: a run of digits, a point, two digits.
AMOUNT = re.compile(r"\d\.\d\d(?!\d)")
D = dt.date
C = Decimal
ZERO = Decimal("0")


def load_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("parity_check", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def ledger(
    owner_conn: Connection, rolled_back_context: MakeContext, tmp_path: Path
) -> Ledger:
    return Ledger(owner_conn, rolled_back_context(Role.OWNER), tmp_path)


@pytest.fixture
def built(ledger: Ledger, rolled_back_context: MakeContext) -> StatementListOut:
    """The ledger: the synthetic statement and bill parsed; returns its statements."""
    catalogue.load_all()
    ledger.accounts(*ACCOUNTS)
    ledger.file(f"vancity/{STATEMENT_END}.pdf", STATEMENT)
    ledger.file("virgin-plus/2026-02-02.pdf", VIRGIN_BILL)
    ledger.derive()
    out = dispatch(
        "finance.statement.list",
        {"parsers": ["vancity.statement", "virgin_plus.bill"]},
        Role.OWNER,
        ctx=rolled_back_context(Role.OWNER),
    )
    assert isinstance(out, StatementListOut)
    return out


@pytest.fixture
def run_script(
    rolled_back_context: MakeContext,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> Callable[[Path], tuple[int, str, str]]:
    ctx = rolled_back_context(Role.OWNER)
    monkeypatch.setattr(composition, "build_context", lambda _role: ctx)
    script = load_script()

    def run(directory: Path) -> tuple[int, str, str]:
        code = script.main([str(directory)])
        captured = capsys.readouterr()
        return code, captured.out, captured.err

    return run


def vancity_rows(out: StatementListOut, *, checks_ok: bool = True) -> list[str]:
    """One CSV row per statement period that moved or held money, as the monitor
    writes them; the name columns hold placeholders."""
    (doc,) = [d for d in out.documents if d.parser_id == "vancity.statement"]
    rows = []
    for n, p in enumerate(doc.periods):
        values = (p.opening, p.total_debits, p.total_credits, p.closing)
        if not any(v for v in values):
            continue
        cells = ["" if v is None else str(v) for v in values]
        rows.append(
            f"{doc.period_start},{doc.period_end},Acct {n},Acct {n},"
            + ",".join(cells)
            + f",,0.0,0.0,0.0,0.0,0.0,0.0,0.0,,,{checks_ok}"
        )
    assert rows
    return rows


def virgin_row(*, total_delta: Decimal = ZERO, source: str = "reference") -> str:
    """The fixture bill as the monitor writes it, from docextract's own reading."""
    bill = reference_bill(VIRGIN_BILL)
    cells = {
        name: money(bill[name])
        for name in ("total_due", "unpaid_balance", "current_charges",
                     "late_payment_charge", "device_payment", "adjustments")
    }  # fmt: skip
    total = cells["total_due"]
    assert total is not None
    cells["total_due"] = total + total_delta
    return ",".join(
        [
            str(bill["bill_date"]),
            str(bill["due_date"]),
            *(str(cells[name]) for name in ("total_due", "unpaid_balance",
                                            "current_charges", "late_payment_charge",
                                            "device_payment", "adjustments")),
            "0.00",
            str(bill["data_used_mb"]),
            source,
            "False",
        ]
    )  # fmt: skip


def write(directory: Path, vancity: list[str], virgin: list[str]) -> Path:
    directory.mkdir()
    (directory / VANCITY_FILE).write_text(
        "\n".join([VANCITY_HEADER, *vancity]) + "\n", encoding="utf-8"
    )
    (directory / VIRGIN_FILE).write_text(
        "\n".join([VIRGIN_HEADER, *virgin]) + "\n", encoding="utf-8"
    )
    return directory


def assert_no_amount(text: str, directory: Path) -> None:
    assert AMOUNT.search(text) is None
    with (directory / VANCITY_FILE).open(encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            for column in ("opening", "withdrawals", "deposits", "closing"):
                if row[column] and C(row[column]) != 0:
                    assert row[column] not in text
            assert row["account"] not in text
    assert chr(0x2014) not in text


# --- the script over a real ledger ---------------------------------------------------


def test_equal_totals_pass(
    built: StatementListOut,
    run_script: Callable[[Path], tuple[int, str, str]],
    tmp_path: Path,
) -> None:
    directory = write(tmp_path / "ref", vancity_rows(built), [virgin_row()])

    code, out, err = run_script(directory)

    assert (code, err) == (0, "")
    assert out.splitlines() == [
        "parity: 2 statements, 2 pass, 0 fail",
        "of the fails, ledger stricter by one cent: 0, ledger laxer than validate: 0;"
        " ledger only: 0",
        "",
        f"{STATEMENT_END} vancity: pass, totals equal",
        "2026-02-02 virgin: pass, totals equal",
    ]
    assert_no_amount(out, directory)


def test_a_one_cent_difference_fails(
    built: StatementListOut,
    run_script: Callable[[Path], tuple[int, str, str]],
    tmp_path: Path,
) -> None:
    rows = vancity_rows(built)
    cells = rows[0].split(",")
    cells[7] = str(C(cells[7]) + C("0.01"))  # the closing balance, one cent up
    rows[0] = ",".join(cells)
    directory = write(tmp_path / "ref", rows, [virgin_row(total_delta=C("-0.01"))])

    code, out, err = run_script(directory)

    assert (code, err) == (1, "")
    assert out.splitlines()[0] == "parity: 2 statements, 0 pass, 2 fail"
    assert f"{STATEMENT_END} vancity: fail, totals differ" in out.splitlines()
    assert "2026-02-02 virgin: fail, totals differ" in out.splitlines()
    assert_no_amount(out, directory)


def test_a_statement_the_parser_rejects_and_the_ledger_accepts_fails(
    built: StatementListOut,
    run_script: Callable[[Path], tuple[int, str, str]],
    tmp_path: Path,
) -> None:
    directory = write(
        tmp_path / "ref",
        vancity_rows(built, checks_ok=False),
        [virgin_row(source="model")],
    )

    code, out, _ = run_script(directory)

    assert code == 1
    words = "fail, the existing parser rejects it, the ledger accepts it"
    assert out.splitlines()[3:] == [
        f"{STATEMENT_END} vancity: {words}",
        f"2026-02-02 virgin: {words}",
    ]
    assert_no_amount(out, directory)


def test_a_statement_both_reject_passes(
    ledger: Ledger,
    run_script: Callable[[Path], tuple[int, str, str]],
    tmp_path: Path,
) -> None:
    # The summary box one cent off: docextract's checks fail on both sides.
    catalogue.load_all()
    ledger.accounts(*ACCOUNTS)
    summary = "642.00          12.45"
    assert STATEMENT.count(summary) == 1
    ledger.file(
        f"vancity/{STATEMENT_END}.pdf",
        STATEMENT.replace(summary, "642.00          12.46"),
    )
    ledger.derive()
    row = (
        f"2025-12-11,{STATEMENT_END},Acct,Acct,150.00,0.0,0.0,12.45,,0.0,0.0,0.0,0.0,"
        "0.0,0.0,0.0,,,False"
    )
    directory = write(tmp_path / "ref", [row], [])

    code, out, _ = run_script(directory)

    assert code == 0
    assert out.splitlines()[3:] == [f"{STATEMENT_END} vancity: pass, both reject it"]


def test_a_statement_the_ledger_lacks_fails_and_one_it_alone_holds_is_named(
    built: StatementListOut,
    run_script: Callable[[Path], tuple[int, str, str]],
    tmp_path: Path,
) -> None:
    moved = [r.replace(STATEMENT_END, "2026-02-10") for r in vancity_rows(built)]
    directory = write(tmp_path / "ref", moved, [virgin_row()])

    code, out, _ = run_script(directory)

    assert code == 1
    assert out.splitlines()[:2] == [
        "parity: 3 statements, 1 pass, 1 fail",
        "of the fails, ledger stricter by one cent: 0, ledger laxer than validate: 0;"
        " ledger only: 1",
    ]
    assert out.splitlines()[3:] == [
        f"{STATEMENT_END} vancity: ledger only, not in the existing parser's output",
        "2026-02-10 vancity: fail, the existing parser accepts it, the ledger lacks it",
        "2026-02-02 virgin: pass, totals equal",
    ]


def test_an_unreadable_reference_stops_with_its_file_and_column_only(
    run_script: Callable[[Path], tuple[int, str, str]], tmp_path: Path
) -> None:
    bad = f"2025-12-11,{STATEMENT_END},Acct,Acct,999.0x,0,0,0,,0,0,0,0,0,0,0,,,True"
    directory = write(tmp_path / "ref", [bad], [])

    code, out, err = run_script(directory)

    assert (code, out) == (1, "")
    assert err == (
        "parity: cannot read the reference. vancity.csv: row 2: column opening is not"
        " an amount\n"
    )


def test_an_unreachable_database_is_one_line(
    make_context: MakeContext,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = int(probe.getsockname()[1])
    nowhere = URL.create(
        "postgresql+psycopg", username="nobody", password="unused",
        host="127.0.0.1", port=port, database="finance_nowhere_test",
    )  # fmt: skip
    ctx = operations.with_databases(
        make_context(Role.OWNER), owner_url=nowhere, reader_url=nowhere
    )
    monkeypatch.setattr(composition, "build_context", lambda _role: ctx)
    directory = write(tmp_path / "ref", [], [])

    assert load_script().main([str(directory)]) == 1
    assert capsys.readouterr().err == "parity: database unreachable\n"


# --- compare and report over synthetic rows ------------------------------------------


def ledger_row(
    key: D, totals: tuple[Totals, ...], *, accepted: bool = True,
    reason: str | None = None, source: str = "virgin",
) -> LedgerStatement:  # fmt: skip
    label = f"{key:%Y-%m}" if source == "virgin" else key.isoformat()
    return LedgerStatement(source, key, label, accepted, reason, totals)  # type: ignore[arg-type]


def reference_row(
    key: D, verdict: str, totals: tuple[Totals, ...], *, source: str = "virgin"
) -> ReferenceStatement:
    return ReferenceStatement(source, key, key.isoformat(), verdict, totals)  # type: ignore[arg-type]


def kinds(results: list[Result]) -> list[Kind]:
    return [each.kind for each in results]


def test_a_bill_within_the_one_cent_tolerance_the_ledger_rejects_fails_by_name() -> (
    None
):
    # Conductor decision: AC-7 is binary, so the named kind still fails.
    month = D(2026, 3, 1)
    ref = reference_row(month, "one_cent", ((C("10.00"),),))

    stricter = compare(
        [ledger_row(month, (), accepted=False, reason="identity_mismatch")], [ref]
    )
    other = compare(
        [ledger_row(month, (), accepted=False, reason="unknown_account")], [ref]
    )

    assert kinds(stricter) == [Kind.FAIL_STRICTER_ONE_CENT]
    assert Kind.FAIL_STRICTER_ONE_CENT.failed
    assert kinds(other) == [Kind.FAIL_LEDGER_REJECTS]
    lines = report(stricter).text().splitlines()
    assert lines[:2] == [
        "parity: 1 statements, 0 pass, 1 fail",
        "of the fails, ledger stricter by one cent: 1, ledger laxer than validate: 0;"
        " ledger only: 0",
    ]
    assert AMOUNT.search("\n".join(lines)) is None


def test_a_bill_validate_flags_and_the_ledger_accepts_fails_and_compares_totals() -> (
    None
):
    month = D(2026, 3, 1)
    ref = reference_row(month, "validate_flag", ((C("10.00"),),))

    assert kinds(compare([ledger_row(month, ((C("10.00"),),))], [ref])) == [
        Kind.FAIL_LAXER_VALIDATE_FLAG
    ]
    assert kinds(compare([ledger_row(month, ((C("10.01"),),))], [ref])) == [
        Kind.FAIL_TOTALS
    ]
    assert kinds(compare([ledger_row(month, (), accepted=False)], [ref])) == [
        Kind.PASS_BOTH_REJECT
    ]
    assert kinds(compare([], [ref])) == [Kind.PASS_BOTH_REJECT]
    assert Kind.FAIL_LAXER_VALIDATE_FLAG.failed
    assert not Kind.LEDGER_ONLY.failed


def test_two_documents_for_one_statement_fail() -> None:
    month = D(2026, 3, 1)
    ref = reference_row(month, "accepted", ((C("10.00"),),))
    twice = [ledger_row(month, ((C("10.00"),),)), ledger_row(month, ((C("10.00"),),))]

    assert kinds(compare(twice, [ref])) == [Kind.FAIL_DUPLICATE]


def test_vancity_totals_compare_as_a_multiset() -> None:
    end = D(2026, 1, 10)
    a = (C("1.00"), C("2.00"), C("3.00"), C("2.00"))
    b = (C("5.00"), C("0.00"), C("1.00"), C("6.00"))
    ref = reference_row(end, "accepted", (a, b), source="vancity")

    assert kinds(compare([ledger_row(end, (b, a), source="vancity")], [ref])) == [
        Kind.PASS_EQUAL
    ]
    assert kinds(compare([ledger_row(end, (a, a), source="vancity")], [ref])) == [
        Kind.FAIL_TOTALS
    ]


# --- the Virgin verdict: what "the existing parser rejects" means -------------------


def bill(**changes: object) -> dict[str, object]:
    fields: dict[str, object] = {
        "bill_date": D(2026, 2, 2),
        "due_date": D(2026, 2, 23),
        "total_due": C("100.00"),
        "unpaid_balance": C("40.00"),
        "current_charges": C("60.00"),
        "late_payment_charge": C("0.00"),
        "device_payment": C("0.00"),
        "adjustments": C("0.00"),
        "data_used_mb": C("10.00"),
        "source": "reference",
    }
    fields.update(changes)
    return fields


@pytest.mark.parametrize(
    ("changes", "verdict"),
    [
        ({}, "accepted"),
        ({"source": "model"}, "rejected"),
        # validate allows one cent on the identity; the ledger allows none.
        ({"total_due": C("100.01")}, "one_cent"),
        ({"total_due": C("99.99")}, "one_cent"),
        ({"total_due": C("100.02")}, "rejected"),
        ({"current_charges": None}, "validate_flag"),
        ({"device_payment": C("-1.00")}, "validate_flag"),
        ({"adjustments": C("1.00")}, "validate_flag"),
        ({"due_date": D(2026, 2, 8)}, "validate_flag"),
        ({"due_date": D(2026, 3, 6)}, "validate_flag"),
        ({"due_date": D(2026, 2, 9)}, "accepted"),
        ({"due_date": D(2026, 3, 5)}, "accepted"),
    ],
)
def test_the_virgin_verdict(changes: dict[str, object], verdict: str) -> None:
    assert virgin_verdict(bill(**changes)) == verdict


def test_read_virgin_keys_a_bill_by_its_month(tmp_path: Path) -> None:
    path = tmp_path / VIRGIN_FILE
    path.write_text(VIRGIN_HEADER + "\n" + virgin_row() + "\n", encoding="utf-8")

    (row,) = read_virgin(path)

    assert (row.key, row.label, row.verdict) == (
        D(2026, 2, 1),
        "2026-02-02",
        "accepted",
    )


def test_read_vancity_rounds_float_noise_to_the_cent(tmp_path: Path) -> None:
    # The monitor writes some amounts from floats; docextract's to_money rounds them
    # to the cent, and so does the reader, so float noise is no difference.
    path = tmp_path / VANCITY_FILE
    row = f"2025-12-11,{STATEMENT_END},Acct,Acct,150.0,0.1,0.30000000000000004,"
    row += "12.449999999999999,,0,0,0,0,0,0,0,,,True"
    path.write_text(VANCITY_HEADER + "\n" + row + "\n", encoding="utf-8")

    (ref,) = read_vancity(path)
    ledger = ledger_row(
        D(2026, 1, 10),
        ((C("150.00"), C("0.10"), C("0.30"), C("12.45")),),
        source="vancity",
    )

    assert ref.totals == ((C("150.00"), C("0.10"), C("0.30"), C("12.45")),)
    assert kinds(compare([ledger], [ref])) == [Kind.PASS_EQUAL]


# --- an all-zero Vancity section: left out on both sides ----------------------------

MONEY_ROW = f"2025-12-11,{STATEMENT_END},Acct,Acct,150.00,10.00,0.00,140.00"
ZERO_ROW = f"2025-12-11,{STATEMENT_END},Acct,Acct,0.0,0.0,0.0,0.0"
ROW_TAIL = ",,0,0,0,0,0,0,0,,,"


def periods_out(*periods: tuple[str, str, str, str]) -> StatementListOut:
    """One parsed Vancity statement document with these (opening, debits, credits,
    closing) periods."""
    return StatementListOut.model_validate(
        {
            "documents": [
                {
                    "parser_id": "vancity.statement",
                    "status": "parsed",
                    "reject_reason": None,
                    "period_start": D(2025, 12, 11),
                    "period_end": D(2026, 1, 10),
                    "periods": [
                        {
                            "opening": C(o),
                            "total_debits": C(d),
                            "total_credits": C(c),
                            "closing": C(z),
                            "balance_due": None,
                            "due_date": None,
                        }  # fmt: skip
                        for o, d, c, z in periods
                    ],
                }
            ]
        }
    )


def vancity_csv(tmp_path: Path, *rows: tuple[str, bool]) -> list[ReferenceStatement]:
    path = tmp_path / VANCITY_FILE
    body = "".join(f"{row}{ROW_TAIL}{ok}\n" for row, ok in rows)
    path.write_text(VANCITY_HEADER + "\n" + body, encoding="utf-8")
    return read_vancity(path)


MONEY_PERIOD = ("150.00", "10.00", "0.00", "140.00")
ZERO_PERIOD = ("0.00", "0.00", "0.00", "0.00")


def test_a_zero_section_on_the_reference_side_only_passes(tmp_path: Path) -> None:
    reference = vancity_csv(tmp_path, (MONEY_ROW, True), (ZERO_ROW, True))
    assert kinds(compare(ledger_statements(periods_out(MONEY_PERIOD)), reference)) == [
        Kind.PASS_EQUAL
    ]


def test_a_zero_section_on_both_sides_passes(tmp_path: Path) -> None:
    reference = vancity_csv(tmp_path, (MONEY_ROW, True), (ZERO_ROW, True))
    ledger = ledger_statements(periods_out(MONEY_PERIOD, ZERO_PERIOD))
    assert kinds(compare(ledger, reference)) == [Kind.PASS_EQUAL]
    # On the ledger side only, too.
    alone = vancity_csv(tmp_path, (MONEY_ROW, True))
    assert kinds(compare(ledger, alone)) == [Kind.PASS_EQUAL]


def test_a_money_section_beside_a_zero_one_is_still_compared(tmp_path: Path) -> None:
    reference = vancity_csv(tmp_path, (MONEY_ROW, True), (ZERO_ROW, True))
    off = ("150.00", "10.00", "0.00", "140.01")
    assert kinds(
        compare(ledger_statements(periods_out(off, ZERO_PERIOD)), reference)
    ) == [Kind.FAIL_TOTALS]


def test_a_zero_section_s_checks_ok_still_decides_the_verdict(tmp_path: Path) -> None:
    (statement,) = vancity_csv(tmp_path, (MONEY_ROW, True), (ZERO_ROW, False))
    assert statement.verdict == "rejected"


def test_a_zero_reference_row_beside_the_real_ledger_passes(
    built: StatementListOut,
    run_script: Callable[[Path], tuple[int, str, str]],
    tmp_path: Path,
) -> None:
    zero = ZERO_ROW + ROW_TAIL + "True"
    directory = write(tmp_path / "ref", [*vancity_rows(built), zero], [virgin_row()])

    code, out, _ = run_script(directory)

    assert code == 0
    assert f"{STATEMENT_END} vancity: pass, totals equal" in out.splitlines()


# --- finance.statement.list ----------------------------------------------------------


def test_statement_list_leaves_out_superseded_and_unparsed_documents(
    ledger: Ledger, rolled_back_context: MakeContext
) -> None:
    catalogue.load_all()
    ledger.accounts(*ACCOUNTS)
    ledger.file(f"vancity/{STATEMENT_END}.pdf", STATEMENT)
    ledger.file(f"vancity/{STATEMENT_END}-2.pdf", STATEMENT + "\n\n")  # a twin
    ledger.file("vancity/notes.csv", "NOT AN EXPORT\n")  # unparsed
    summary = "642.00          12.45"
    ledger.file(
        "vancity/2026-01-11.pdf", STATEMENT.replace(summary, "642.00          12.46")
    )  # rejected
    ledger.derive()

    out = dispatch(
        "finance.statement.list",
        {"parsers": ["vancity.statement"]},
        Role.OWNER,
        ctx=rolled_back_context(Role.OWNER),
    )

    assert isinstance(out, StatementListOut)
    got = sorted((d.status, d.reject_reason, len(d.periods)) for d in out.documents)
    assert got[0][:2] == ("parsed", None) and got[0][2] > 0
    assert got[1] == ("rejected", "checks_failed", 0)
    assert len(got) == 2


@pytest.mark.parametrize("role", [Role.MEMBER, Role.SERVICE])
def test_statement_list_is_the_owner_s_alone(
    role: Role, rolled_back_context: MakeContext
) -> None:
    spec = operations.SPECS["finance.statement.list"]
    assert (spec.connection, spec.roles, spec.tool_name) == (
        "owner", frozenset({Role.OWNER}), None,
    )  # fmt: skip
    with pytest.raises(operations.RoleRefused):
        dispatch(
            "finance.statement.list",
            {"parsers": ["vancity.statement"]},
            role,
            ctx=rolled_back_context(role),
        )


def test_statement_list_needs_a_parser(rolled_back_context: MakeContext) -> None:
    with pytest.raises(operations.InputInvalid):
        dispatch(
            "finance.statement.list",
            {"parsers": []},
            Role.OWNER,
            ctx=rolled_back_context(Role.OWNER),
        )


# --- the script folds unreadable input and comparison failures into one line ------


def test_a_reference_that_is_not_utf8_is_one_line(
    run_script: Callable[[Path], tuple[int, str, str]], tmp_path: Path
) -> None:
    directory = write(tmp_path / "ref", [], [])
    (directory / VANCITY_FILE).write_bytes(VANCITY_HEADER.encode() + b"\n\xff\xfe\n")

    code, out, err = run_script(directory)

    assert (code, out) == (1, "")
    assert err == "parity: cannot read the reference. vancity.csv: not UTF-8 text\n"


def test_a_reference_that_is_not_valid_csv_names_the_row_only(
    run_script: Callable[[Path], tuple[int, str, str]], tmp_path: Path
) -> None:
    directory = write(tmp_path / "ref", [], [])
    secret = "999123.45"
    (directory / VANCITY_FILE).write_text(
        VANCITY_HEADER + "\n" + f'x,"{secret}\n', encoding="utf-8"
    )
    # A field over the reader's size limit: csv.Error, whose text quotes nothing
    # here, but the line must name the row only whatever csv says.
    limit = csv.field_size_limit(4)
    try:
        code, out, err = run_script(directory)
    finally:
        csv.field_size_limit(limit)

    assert (code, out) == (1, "")
    assert err.startswith("parity: cannot read the reference. vancity.csv: row ")
    assert err.endswith(": not valid CSV\n")
    assert secret not in err


def test_a_failure_inside_the_comparison_is_one_line(
    built: StatementListOut,
    run_script: Callable[[Path], tuple[int, str, str]],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(*_args: object) -> list[Result]:
        raise ValueError("a value 999123.45 that must not print")

    monkeypatch.setattr(parity, "compare", boom)
    directory = write(tmp_path / "ref", vancity_rows(built), [virgin_row()])

    code, out, err = run_script(directory)

    assert (code, out) == (1, "")
    assert err == "parity: cannot compare. ValueError\n"
