"""Public owed CLI over synthetic scratch data and an unusable owner DSN."""

from __future__ import annotations

import dataclasses
import datetime as dt
import json
from collections.abc import Callable, Iterator
from decimal import Decimal
from pathlib import Path
from types import MappingProxyType

import pytest
from sqlalchemy import Connection, create_engine, insert, select, text, update
from sqlalchemy.pool import NullPool

from conftest import DatabaseUnderTest
from derive.support import Ledger
from rheo_finance import catalogue, composition, operations
from rheo_finance.cli.main import main
from rheo_finance.contracts import Role
from rheo_finance.documents import issuers
from rheo_finance.storage.ids import uuid7
from rheo_finance.storage.schema import (
    decision,
    document,
    document_line,
    obligation,
    open_item,
    payee,
    payment,
    payment_application,
    payment_evidence,
)
from test_owed_ops import NOW, Built, add, doc, reader

M = Decimal
Ran = tuple[int, str, str]


@pytest.fixture(scope="module")
def built(
    scratch_database: DatabaseUnderTest, tmp_path_factory: pytest.TempPathFactory
) -> Built:
    catalogue.load_all()
    return scratch_database, tmp_path_factory.mktemp("owed-cli")


@pytest.fixture
def stored(built: Built) -> Iterator[Connection]:
    database, _ = built
    engine = create_engine(database.owner_url, poolclass=NullPool)
    try:
        with engine.connect() as conn:
            try:
                yield conn
            finally:
                conn.rollback()
                assert (database.owner_url.database or "").startswith(
                    "finance_scratch_"
                )
                conn.execute(
                    text(
                        "TRUNCATE finance.payee, finance.document, "
                        "finance.account CASCADE"
                    )
                )
                conn.commit()
    finally:
        engine.dispose()


@pytest.fixture
def run(
    built: Built, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> Callable[..., Ran]:
    ctx = dataclasses.replace(reader(built), role=Role.OWNER)

    def context(role: Role) -> object:
        assert role == Role.OWNER
        return ctx

    monkeypatch.setattr(composition, "build_context", context)
    original = operations.dispatch

    def dispatch(
        name: str, payload: dict[str, object], role: Role, **kwargs: object
    ) -> object:
        assert role == Role.OWNER
        assert name in {"finance.owed.list", "finance.owed.trace"}
        assert operations.SPECS[name].connection == "reader"
        return original(name, payload, role, ctx=ctx)

    def go(*args: str) -> Ran:
        with monkeypatch.context() as patch:
            patch.setattr(operations, "dispatch", dispatch)
            code = main(["owed", *args])
        capture = capsys.readouterr()
        assert chr(0x2014) not in capture.out + capture.err
        return code, capture.out, capture.err

    return go


def output(run: Callable[..., Ran], *args: str) -> str:
    code, out, err = run(*args)
    assert (code, err) == (0, "")
    return out


def test_default_headline_cap_totals_card_columns_order_and_json(
    stored: Connection, run: Callable[..., Ran]
) -> None:
    add(stored, paid="10.00", before_floor=True)
    for n in range(15):
        add(
            stored,
            f"sample-{n:02}",
            "bill",
            dt.date(2026, 5, 25),
            amount="30",
            minimum=None,
        )
    add(stored, "undated", "manual_debt", None, amount="40", minimum=None)
    stored.commit()
    got = json.loads(output(run, "--json"))
    shown = output(run)
    head, detail = shown.split("\n\n", 1)
    assert head.splitlines() == got["headline"]
    assert len(head.splitlines()) == 5
    assert len(got["rows"]) == 15 and got["more"] == 2
    assert got["due_in_window"] == "450.00"
    assert "due in the next 14 days: 450.00 CAD" in head
    assert "overdue: 15.00 CAD" in head and "carried: 775.00 CAD" in head
    rows = [
        line for line in detail.splitlines() if line.startswith("finance.obligation:")
    ]
    assert len(rows) == 15 and "+2 more" in detail.splitlines()
    assert got["rows"][0]["kind"] == "card_statement"
    assert rows[0].endswith(
        "source sample-bills/sample-card.pdf; before reminders floor"
    )
    for fragment in [
        "balance 800.00 CAD",
        "minimum 25.00 CAD",
        "paid 10.00 CAD",
        "overdue 15.00 CAD",
        "carried 775.00 CAD",
        "remaining 790.00 CAD",
        "due 2026-05-20",
        "1 days overdue",
    ]:
        assert fragment in rows[0]
    assert not got["rows"][0]["reminder_eligible"]
    for row, line in zip(got["rows"], rows, strict=True):
        assert row["primary_source"] in line
        assert row["payee_label"] in line and row["obligation_ref"] in line
    assert "4 days remaining" in rows[1]
    assert detail.index("+2 more") < detail.index("credits not applied: 0")


def test_undated_manual_source_and_missing_card_minimum(
    stored: Connection, run: Callable[..., Ran]
) -> None:
    add(stored, minimum=None)
    ident = add(stored, "manual", "manual_debt", None, minimum=None)
    stored.execute(
        update(obligation)
        .where(obligation.c.id == ident)
        .values(primary_source="obligations.csv:2", manual_source="obligations.csv:2")
    )
    stored.commit()
    got = json.loads(output(run, "--json"))
    shown = output(run)
    assert "minimum not printed (threshold 800.00 CAD)" in shown
    assert got["rows"][-1]["due_date"] is None
    assert "no due date; source obligations.csv:2" in shown


def test_gaps_alone_periods_and_json_parity(
    stored: Connection, run: Callable[..., Ran], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        issuers,
        "ISSUER_DIRS",
        MappingProxyType(
            {**issuers.ISSUER_DIRS, "sample-bills": issuers.IssuerDir("bill")}
        ),
    )
    ident = doc(stored, "sample-bills/photo.jpg", "image", "needs_manual_entry")
    stored.execute(
        update(document)
        .where(document.c.id == ident)
        .values(period_start=dt.date(2026, 4, 1), period_end=dt.date(2026, 4, 30))
    )
    doc(stored, "sample-bills/unparsed.pdf", "bill", "unparsed")
    add(stored)
    stored.commit()
    full = json.loads(output(run, "--json"))
    gaps = json.loads(output(run, "--gaps", "--json"))
    assert gaps == {"gaps": full["gaps"], "gap_count": 2}
    shown = output(run, "--gaps")
    assert shown.splitlines() == [
        "bill gaps: 2",
        "",
        "gap: sample-bills/photo.jpg; sample-bills; image; needs_manual_entry; "
        "period 2026-04-01 to 2026-04-30",
        "gap: sample-bills/unparsed.pdf; sample-bills; bill; unparsed",
    ]
    assert (
        "overdue" not in shown
        and "credits" not in shown
        and "finance.obligation:" not in shown
    )
    full_text = output(run)
    assert full_text.index("credits not applied: 0") < full_text.index(
        "gap: sample-bills/photo.jpg"
    )


def test_trace_both_directions_every_application_and_evidence(
    stored: Connection, run: Callable[..., Ran]
) -> None:
    old = add(stored, status="superseded", paid="25", manual_mismatch=M("2"))
    current = add(
        stored, period="card-2026-05-25", supersedes_id=old, payments_mismatch=M("5")
    )
    pid = stored.scalar(select(payee.c.id))
    for n in range(2):
        pay, app = uuid7(NOW), uuid7(NOW)
        stored.execute(
            insert(payment).values(
                id=pay,
                payee_id=pid,
                amount=M("12.50"),
                currency="CAD",
                paid_on=dt.date(2026, 5, 18 + n),
                source="money",
                state="active",
            )
        )
        stored.execute(
            insert(payment_application).values(
                id=app, payment_id=pay, obligation_id=old, amount=M("12.50"), auto=True
            )
        )
        for evidence_n in range(2):
            did = doc(
                stored,
                f"sample-notices/{n}-{evidence_n}.eml",
                "email",
                "parsed",
                "sample-notices",
            )
            line_id = uuid7(NOW)
            stored.execute(
                insert(document_line).values(
                    id=line_id,
                    document_id=did,
                    line_no=1,
                    line_kind="payment_notice",
                    posting_date=dt.date(2026, 5, 18),
                    amount=M("12.50"),
                    currency="CAD",
                )
            )
            stored.execute(
                insert(payment_evidence).values(
                    id=uuid7(NOW),
                    payment_id=pay,
                    stream_key=f"sample-{n}-{evidence_n}",
                    role="notice",
                    document_line_id=line_id,
                )
            )
    stored.commit()
    for ident in (old, current):
        ref = f"finance.obligation:{ident}"
        got = json.loads(output(run, "--trace", ref, "--json"))
        shown = output(run, "--trace", ref)
        assert len(got["obligations"]) == 2
        assert shown.count("  application ") == 2
        assert shown.count("    evidence ") == 4
        assert "manual_mismatch: 2.00 CAD" in shown
        assert "payments_mismatch: 5.00 CAD" in shown
        assert "status superseded; recorded paid 25.00 CAD" in shown
        assert f"supersedes finance.obligation:{old}" in shown
        for item in got["obligations"]:
            assert item["row"]["primary_source"] in shown
            for app in item["applications"]:
                assert shown.count(f"application {app['application_ref']};") == 1
                assert (
                    f"payment {app['payment_ref']}; {app['paid_on']}; applied 12.50 CAD"
                    in shown
                )
                for ev in app["evidence"]:
                    assert shown.count(f"evidence {ev['evidence_ref']};") == 1
                    assert ev["document_path"] in shown


def test_presumed_paid_effective_and_recorded_figures(
    stored: Connection, run: Callable[..., Ran]
) -> None:
    ident = add(stored, status="presumed_paid")
    did = uuid7(NOW)
    ref = f"finance.obligation:{ident}"
    stored.execute(
        insert(decision).values(
            id=did, kind="presumed_paid", obligation_ref=ref, decided_at=NOW
        )
    )
    stored.execute(
        update(obligation)
        .where(obligation.c.id == ident)
        .values(presumed_paid_decision_id=did)
    )
    stored.commit()
    shown = output(run, "--trace", ref)
    assert "paid 800.00 CAD" in shown and "remaining 0.00 CAD" in shown
    assert "recorded paid 0.00 CAD; recorded remaining 800.00 CAD" in shown
    assert f"presumed-paid basis finance.decision:{did}; owner; presumed_paid;" in shown


def test_credits_preserve_currencies_missing_cad_and_payee_only_reason(
    stored: Connection, run: Callable[..., Ran]
) -> None:
    ident = add(stored)
    pid = stored.scalar(select(payee.c.id))
    for currency, amount, cad in [("CAD", "5", "5"), ("USD", "7", None)]:
        pay, app = uuid7(NOW), uuid7(NOW)
        stored.execute(
            insert(payment).values(
                id=pay,
                payee_id=pid,
                amount=M(amount),
                currency=currency,
                paid_on=dt.date(2026, 5, 18),
                source="notice_only",
                state="active",
            )
        )
        stored.execute(
            insert(payment_application).values(
                id=app, payment_id=pay, amount=M(amount), reason="excess", auto=True
            )
        )
        stored.execute(
            insert(open_item).values(
                id=uuid7(NOW),
                tax_year=2026,
                kind="unapplied_credit",
                subject_ref=f"finance.payment_application:{app}",
                payment_id=pay,
                occurred_on=dt.date(2026, 5, 18),
                amount=-M(amount),
                direction="out",
                currency=currency,
                cad_amount=None if cad is None else -M(cad),
                state="unapplied",
                reason_code="excess",
                reason_payee_id=pid,
            )
        )
    stored.commit()
    got = json.loads(output(run, "--json"))
    assert got["overdue"] == "25.00" and got["carried"] == "775.00"
    assert got["credits"]["count"] == 2
    assert got["credits"]["sums"][1]["cad_amount"] is None
    shown = output(run, "--trace", f"finance.obligation:{ident}")
    assert "credits not applied: 2, 5.00 CAD; 7.00 USD" in shown
    assert "5.00 CAD; credit exceeds obligation for Sample-Card" in shown
    assert "7 USD; credit exceeds obligation for Sample-Card" in shown
    for row in got["credits"]["items"]:
        assert row["payment_ref"] in shown and row["line_ref"] in shown
        assert row["occurred_on"] in shown and row["reason"] in shown


def test_patternless_invoice_hint_uses_canonical_folder(
    built: Built, stored: Connection, run: Callable[..., Ran], tmp_path: Path
) -> None:
    from derive.test_invoice_decisions import unpaid
    from derive.test_receipts_invoices import CONTRACTOR, INVOICE, invoice, payments

    database, _ = built
    ctx = dataclasses.replace(reader(built), role=Role.OWNER)
    ctx = operations.with_databases(
        ctx, owner_url=database.owner_url, reader_url=database.reader_url
    )
    ledger = Ledger(stored, operations.with_owner_connection(ctx, stored), tmp_path)
    payments(ledger, ("10-Mar-2026", CONTRACTOR, "650.00"))
    ledger.file(INVOICE, invoice())
    ledger.derive()
    unpaid(ledger)
    ledger.derive()
    stored.commit()
    got = json.loads(output(run, "--json"))
    (row,) = got["rows"]
    assert row["payments_unmatchable"]
    folder = row["invoice_folder"]
    assert folder.startswith("invoices-received/")
    assert (
        f"{folder}: payments cannot be matched until this vendor is in payees.csv"
        in output(run)
    )
    shown = output(run, "--trace", row["obligation_ref"])
    assert "no payment can reach this obligation until vendor registration" in shown
    assert CONTRACTOR not in shown


def test_empty_and_unknown_trace_are_explicit(run: Callable[..., Ran]) -> None:
    assert "no open obligations" in output(run)
    assert output(run, "--gaps") == "bill gaps: 0\n"
    assert "finance owed trace: 0 obligations" in output(run, "--trace", "missing")


@pytest.mark.parametrize(
    "argv", [[], ["--json"], ["--trace", "missing"], ["--trace", "missing", "--json"]]
)
def test_sourceless_row_refuses_before_any_output(
    built: Built,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    argv: list[str],
) -> None:
    from rheo_finance.reports.owed import Facts, TraceObligation, TraceOut, figures
    from test_owed_ops import owed

    ctx = dataclasses.replace(reader(built), role=Role.OWNER)
    monkeypatch.setattr(composition, "build_context", lambda _role: ctx)
    empty = owed(built)
    row = figures(
        Facts(
            obligation_ref="sample",
            payee_slug="sample",
            payee_label="Sample",
            kind="bill",
            period_label="sample",
            statement_date=None,
            issued_on=None,
            amount_due=M("5"),
            minimum_due=None,
            threshold_due=M("5"),
            paid=M("0"),
            remaining=M("5"),
            threshold_remaining=M("5"),
            due_date=None,
            status="open",
            primary_source=" ",
            manual_source=None,
            before_floor=False,
            payments_unmatchable=False,
            manual_mismatch=None,
            payments_mismatch=None,
            statement_conflict=False,
            invoice_ref=None,
            arrangement_ref=None,
            supersedes_ref=None,
            presumed_paid_basis=None,
        ),
        dt.date(2026, 5, 21),
        14,
    )
    result = (
        TraceOut(
            obligations=[
                TraceObligation(
                    row=row,
                    basis_class=None,
                    presumed_basis_kind=None,
                    presumed_at=None,
                    applications=[],
                )
            ],
            credits=empty.credits,
        )
        if "--trace" in argv
        else empty.model_copy(update={"rows": [row]})
    )
    monkeypatch.setattr(operations, "dispatch", lambda *_a, **_kw: result)
    assert main(["owed", *argv]) == 1
    got = capsys.readouterr()
    assert got.out == "" and "owed row has no source reference" in got.err


def test_modes_are_mutually_exclusive(run: Callable[..., Ran]) -> None:
    code, out, err = run("--trace", "sample", "--gaps")
    assert code == 2 and out == "" and "not allowed with argument" in err


def test_text_and_json_guard_fixture_strings(
    stored: Connection, run: Callable[..., Ran]
) -> None:
    add(stored)
    stored.execute(
        update(payee).values(display_label="Sample " + chr(0x2014) + " Card")
    )
    stored.commit()
    got = json.loads(output(run, "--json"))
    assert got["rows"][0]["payee_label"] == "Sample - Card"
    assert "Sample - Card" in output(run)
