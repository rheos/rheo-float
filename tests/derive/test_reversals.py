"""Derive stage 8: a reversed payment nets to nothing, in both directions (FR-15(a),
AC-58, edge cases 33, 63 and 81).

Seams: ``derive.run`` through the derive ``ledger`` over a synthetic Vancity chequing
export, read by the real parser with the repo's reversal wording, then read back as
the txn states, the ``pair_edge`` rows and ``ledger.totals.cad_total`` over the lines
that count (``transfer_state`` null); and a guard that stage 8 never reads a
counterparty, payee, party or alias, by the SQL it sends and by its source. Two shapes
come from real kinds of event, built here with invented dates, amounts and names: an
outgoing reversal (a payment sent, cancelled, and sent again) and a clawed-back credit
(a deposit received, taken back, and received again). The outgoing shape also runs on
the credit-union parser's own synthetic statement, and the matching rules on the card
fixtures (an export line joined to a statement line), the conversion fixtures (one
account, two currencies) and a receipt that is a payment's evidence source.
"""

from __future__ import annotations

import ast
import datetime as dt
import inspect
from collections import defaultdict
from collections.abc import Iterator
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import ColumnElement, event, func, select

from rheo_finance.derive import pairing, stage8
from rheo_finance.ledger.totals import cad_total
from rheo_finance.manual.transfer_hints import TRANSFER_HINTS_FILE
from rheo_finance.storage.schema import pair_edge, txn, txn_source

from .support import (
    CARD_IDENTIFIER,
    CARD_STATEMENT,
    CARD_TRANSACTIONS,
    CSV_IDENTIFIER,
    FIXTURES,
    Ledger,
    fx_lines,
)
from .test_receipts_invoices import CLINIC, RECEIPT, debit_only_receipt

D = dt.date
OTHER_IDENTIFIER = "999000000088-999-99901-99902"
OPENING = Decimal("5000.00")

Row = tuple[str, dt.date, str, Decimal]
State = tuple[dt.date, Decimal, str | None, str | None]
Edge = tuple[dt.date, Decimal, dt.date, Decimal, bool]


@dataclass(frozen=True)
class Shape:
    """One direction: the original line's wording and signed amount, and the wording
    of the reversal that undoes it."""

    original: str
    reversal: str
    amount: Decimal


OUTGOING = Shape(
    "INTERAC E-TRANSFER  To: SAMPLE LANDLORD",
    "E-TRANSFER DEBIT REVERSAL CUSTOMER CANCEL",
    Decimal("-875.00"),
)
CLAWED_BACK = Shape(
    "INTERAC E-TRANSFER  From: SAMPLE PAYER",
    "E-TRANSFER CREDIT REVERSAL",
    Decimal("420.00"),
)
DIRECTIONS = pytest.mark.parametrize(
    "shape", [OUTGOING, CLAWED_BACK], ids=["outgoing", "clawed-back"]
)


def export(*rows: Row) -> str:
    """A headerless Vancity chequing export of ``(identifier, day, description,
    signed amount)`` rows, oldest first, each account's balance running from
    ``OPENING``."""
    balances: dict[str, Decimal] = defaultdict(lambda: OPENING)
    found = []
    for identifier, day, description, amount in rows:
        balances[identifier] += amount
        out = f"{-amount:.2f}" if amount < 0 else ""
        into = f"{amount:.2f}" if amount > 0 else ""
        found.append(
            f'{identifier},{day.strftime("%d-%b-%Y")},"{description}",,{out},{into},'
            f"{balances[identifier]:.2f}"
        )
    return "\n".join(found) + "\n"


def line(day: dt.date, description: str, amount: Decimal) -> Row:
    return (CSV_IDENTIFIER, day, description, amount)


@pytest.fixture
def chequing(ledger: Ledger) -> Ledger:
    """Two chequing accounts at Vancity: ``chequing`` and ``second``."""
    ledger.accounts(
        ("chequing", "Vancity", "chequing", CSV_IDENTIFIER),
        ("second", "Vancity", "chequing", OTHER_IDENTIFIER),
    )
    return ledger


def derive(ledger: Ledger, *rows: Row) -> None:
    ledger.file("vancity/export.csv", export(*rows))
    ledger.derive()


def states(ledger: Ledger) -> list[State]:
    rows = ledger.conn.execute(
        select(txn.c.posting_date, txn.c.amount, txn.c.transfer_state, txn.c.pair_kind)
    )
    return sorted(tuple(row) for row in rows)


def edges(ledger: Ledger) -> list[Edge]:
    a, b = txn.alias("a"), txn.alias("b")
    rows = ledger.conn.execute(
        select(
            a.c.posting_date,
            a.c.amount,
            b.c.posting_date,
            b.c.amount,
            pair_edge.c.applied,
        )
        .join(a, a.c.id == pair_edge.c.a_txn_id)
        .join(b, b.c.id == pair_edge.c.b_txn_id)
        .where(pair_edge.c.kind == "reversal")
    )
    return sorted(tuple(row) for row in rows)


def counted(ledger: Ledger, *, debits: bool) -> Decimal:
    """The CAD sum of the lines a spending (debits) or income total reads: those whose
    ``transfer_state`` is null."""
    sign = txn.c.amount < 0 if debits else txn.c.amount > 0
    return cad_total(ledger.uow, [txn.c.transfer_state.is_(None), sign]).sum


def assert_counterparty(
    ledger: Ledger, reversal: ColumnElement[bool], printed: str | None
) -> None:
    """The reversal line's counterparty holds the name it prints, and no sample name
    when it prints none."""
    found = ledger.conn.scalar(select(txn.c.counterparty).where(reversal)) or ""
    if printed is None:
        assert "sample" not in found.casefold()
    else:
        assert printed.casefold() in found.casefold()


# --- the two real shapes --------------------------------------------------------------


@pytest.mark.parametrize(
    ("reversal", "printed"),
    [
        (OUTGOING.reversal, None),
        (OUTGOING.reversal + "  To: SAMPLE LANDLORD", "SAMPLE LANDLORD"),
        (OUTGOING.reversal + "  To: SAMPLE OTHER", "SAMPLE OTHER"),
    ],
    ids=["no-name", "same-name", "other-name"],
)
def test_an_outgoing_reversal_nets_the_cancelled_debit_and_the_resend_counts_once(
    chequing: Ledger, reversal: str, printed: str | None
) -> None:
    sent, cancelled = D(2025, 9, 2), D(2025, 9, 8)
    derive(
        chequing,
        line(sent, OUTGOING.original, Decimal("-875.00")),
        line(cancelled, reversal, Decimal("875.00")),
        line(cancelled, OUTGOING.original, Decimal("-875.00")),
    )

    # The control: the name the reversal prints, or none, reaches the counterparty.
    assert_counterparty(chequing, txn.c.amount > 0, printed)
    # Whatever it prints, the result is the same.
    assert states(chequing) == [
        (sent, Decimal("-875.00"), "paired", "reversal"),
        (cancelled, Decimal("-875.00"), None, None),
        (cancelled, Decimal("875.00"), "paired", "reversal"),
    ]
    assert edges(chequing) == [
        (cancelled, Decimal("875.00"), sent, Decimal("-875.00"), True),
    ]
    # The re-sent debit is the one payment; the reversal is no income.
    assert counted(chequing, debits=True) == Decimal("-875.00")
    assert counted(chequing, debits=False) == Decimal("0.00")


@pytest.mark.parametrize(
    ("reversal", "printed"),
    [
        (CLAWED_BACK.reversal, None),
        (CLAWED_BACK.reversal + "  From: SAMPLE PAYER", "SAMPLE PAYER"),
        (CLAWED_BACK.reversal + "  From: SAMPLE OTHER", "SAMPLE OTHER"),
    ],
    ids=["no-name", "same-name", "other-name"],
)
def test_a_clawed_back_credit_nets_and_the_resent_credit_counts_once(
    chequing: Ledger, reversal: str, printed: str | None
) -> None:
    received, taken_back, again = D(2025, 11, 10), D(2025, 11, 12), D(2025, 11, 13)
    derive(
        chequing,
        line(received, CLAWED_BACK.original, Decimal("420.00")),
        line(taken_back, reversal, Decimal("-420.00")),
        line(again, CLAWED_BACK.original, Decimal("420.00")),
    )

    assert_counterparty(chequing, txn.c.amount < 0, printed)
    assert states(chequing) == [
        (received, Decimal("420.00"), "paired", "reversal"),
        (taken_back, Decimal("-420.00"), "paired", "reversal"),
        (again, Decimal("420.00"), None, None),
    ]
    assert edges(chequing) == [
        (taken_back, Decimal("-420.00"), received, Decimal("420.00"), True),
    ]
    # The re-sent credit is the one deposit; the clawback is no spending.
    assert counted(chequing, debits=False) == Decimal("420.00")
    assert counted(chequing, debits=True) == Decimal("0.00")


# --- the window and the same day ------------------------------------------------------


@pytest.mark.parametrize(
    ("days_before", "paired"), [(31, True), (35, True), (36, False)]
)
def test_an_original_pairs_up_to_the_reversal_window_before(
    chequing: Ledger, days_before: int, paired: bool
) -> None:
    cancelled = D(2025, 10, 20)
    sent = cancelled - dt.timedelta(days=days_before)
    derive(
        chequing,
        line(sent, OUTGOING.original, Decimal("-875.00")),
        line(cancelled, OUTGOING.reversal, Decimal("875.00")),
    )

    if paired:
        assert states(chequing) == [
            (sent, Decimal("-875.00"), "paired", "reversal"),
            (cancelled, Decimal("875.00"), "paired", "reversal"),
        ]
        assert edges(chequing) == [
            (cancelled, Decimal("875.00"), sent, Decimal("-875.00"), True),
        ]
    else:
        assert states(chequing) == [
            (sent, Decimal("-875.00"), None, None),
            (cancelled, Decimal("875.00"), "unresolved", None),
        ]
        assert edges(chequing) == []


@DIRECTIONS
def test_a_same_day_only_candidate_leaves_the_reversal_ambiguous(
    chequing: Ledger, shape: Shape
) -> None:
    day = D(2025, 7, 15)
    derive(
        chequing,
        line(day, shape.reversal, -shape.amount),
        line(day, shape.original, shape.amount),
    )

    # Offered with its one candidate, never netted: the original stays an ordinary
    # line.
    assert sorted(states(chequing)) == sorted(
        [(day, -shape.amount, "ambiguous", None), (day, shape.amount, None, None)]
    )
    assert edges(chequing) == [(day, -shape.amount, day, shape.amount, False)]


@DIRECTIONS
def test_an_earlier_candidate_wins_over_a_same_day_one(
    chequing: Ledger, shape: Shape
) -> None:
    earlier, day = D(2025, 7, 10), D(2025, 7, 15)
    derive(
        chequing,
        line(earlier, shape.original, shape.amount),
        line(day, shape.reversal, -shape.amount),
        line(day, shape.original, shape.amount),
    )

    assert sorted(states(chequing)) == sorted(
        [
            (earlier, shape.amount, "paired", "reversal"),
            (day, -shape.amount, "paired", "reversal"),
            (day, shape.amount, None, None),
        ]
    )
    assert edges(chequing) == [(day, -shape.amount, earlier, shape.amount, True)]


# --- ambiguity, none, and what is never a candidate -----------------------------------


@DIRECTIONS
def test_two_earlier_equal_originals_leave_every_line_ambiguous(
    chequing: Ledger, shape: Shape
) -> None:
    first, second, day = D(2025, 6, 2), D(2025, 6, 9), D(2025, 6, 16)
    derive(
        chequing,
        line(first, shape.original, shape.amount),
        line(second, shape.original, shape.amount),
        line(day, shape.reversal, -shape.amount),
    )

    assert sorted(states(chequing)) == sorted(
        [
            (first, shape.amount, "ambiguous", None),
            (second, shape.amount, "ambiguous", None),
            (day, -shape.amount, "ambiguous", None),
        ]
    )
    assert edges(chequing) == [
        (day, -shape.amount, first, shape.amount, False),
        (day, -shape.amount, second, shape.amount, False),
    ]
    assert counted(chequing, debits=True) == counted(chequing, debits=False) == 0


@DIRECTIONS
def test_two_reversals_of_one_original_leave_every_line_ambiguous(
    chequing: Ledger, shape: Shape
) -> None:
    sent, one, two = D(2025, 6, 2), D(2025, 6, 9), D(2025, 6, 16)
    derive(
        chequing,
        line(sent, shape.original, shape.amount),
        line(one, shape.reversal, -shape.amount),
        line(two, shape.reversal, -shape.amount),
    )

    assert {state for _, _, state, _ in states(chequing)} == {"ambiguous"}
    assert [applied for *_, applied in edges(chequing)] == [False, False]


@DIRECTIONS
def test_a_reversal_with_no_candidate_is_unresolved(
    chequing: Ledger, shape: Shape
) -> None:
    # The earlier line is a cent off: not exactly the amount, so no candidate.
    earlier, day = D(2025, 5, 5), D(2025, 5, 12)
    near = shape.amount + Decimal("0.01")
    derive(
        chequing,
        line(earlier, shape.original, near),
        line(day, shape.reversal, -shape.amount),
    )

    assert sorted(states(chequing)) == sorted(
        [(earlier, near, None, None), (day, -shape.amount, "unresolved", None)]
    )
    assert edges(chequing) == []


@DIRECTIONS
def test_an_original_on_another_account_never_pairs(
    chequing: Ledger, shape: Shape
) -> None:
    earlier, day = D(2025, 5, 5), D(2025, 5, 12)
    derive(
        chequing,
        (OTHER_IDENTIFIER, earlier, shape.original, shape.amount),
        line(day, shape.reversal, -shape.amount),
    )

    assert sorted(states(chequing)) == sorted(
        [(earlier, shape.amount, None, None), (day, -shape.amount, "unresolved", None)]
    )
    assert edges(chequing) == []


@DIRECTIONS
def test_an_equal_line_without_reversal_wording_is_not_a_reversal(
    chequing: Ledger, shape: Shape
) -> None:
    earlier, day = D(2025, 5, 5), D(2025, 5, 12)
    derive(
        chequing,
        line(earlier, shape.original, shape.amount),
        line(day, "DEPOSIT   SAMPLE REFUND  99971", -shape.amount),
    )

    assert sorted(states(chequing)) == sorted(
        [(earlier, shape.amount, None, None), (day, -shape.amount, None, None)]
    )
    assert edges(chequing) == []


def test_a_reversal_line_is_never_the_original_of_another(chequing: Ledger) -> None:
    # A cancelled payment, then a debit with clawback wording of the same amount: the
    # credit reversal is not an original the debit can undo, so the first pair stands
    # and the debit has no candidate.
    sent, cancelled, later = D(2025, 8, 18), D(2025, 8, 21), D(2025, 8, 24)
    derive(
        chequing,
        line(sent, OUTGOING.original, Decimal("-875.00")),
        line(cancelled, OUTGOING.reversal, Decimal("875.00")),
        line(later, CLAWED_BACK.reversal, Decimal("-875.00")),
    )

    assert states(chequing) == [
        (sent, Decimal("-875.00"), "paired", "reversal"),
        (cancelled, Decimal("875.00"), "paired", "reversal"),
        (later, Decimal("-875.00"), "unresolved", None),
    ]
    assert edges(chequing) == [
        (cancelled, Decimal("875.00"), sent, Decimal("-875.00"), True),
    ]


@pytest.mark.parametrize("with_row", [True, False])
def test_a_reversal_row_of_transfer_hints_marks_a_reversal_line(
    chequing: Ledger, with_row: bool
) -> None:
    if with_row:
        chequing.manual(TRANSFER_HINTS_FILE, "sample bank adjustment,reversal,")
    sent, adjusted = D(2025, 4, 1), D(2025, 4, 4)
    derive(
        chequing,
        line(sent, "POS PURCHASE   SAMPLE SHOP  99972", Decimal("-64.00")),
        line(adjusted, "SAMPLE BANK ADJUSTMENT  99973", Decimal("64.00")),
    )

    state, kind = ("paired", "reversal") if with_row else (None, None)
    assert states(chequing) == [
        (sent, Decimal("-64.00"), state, kind),
        (adjusted, Decimal("64.00"), state, kind),
    ]


@pytest.mark.parametrize(("original", "paired"), [("CAD", True), ("USD", False)])
def test_an_equal_amount_in_another_currency_is_no_candidate(
    fx_ledger: Ledger, original: str, paired: bool
) -> None:
    # One account holding two currencies: the CAD reversal undoes a CAD original of
    # its amount, never the same figure in USD.
    fx_ledger.manual(TRANSFER_HINTS_FILE, "sample bank adjustment,reversal,")
    sent, adjusted = D(2025, 8, 4), D(2025, 8, 7)
    fx_ledger.file(
        "synthetic-fx/original.txt",
        fx_lines(original, "2025-08-04 -50.00 - - SAMPLE SHOP 99941"),
    )
    fx_ledger.file(
        "synthetic-fx/adjustment.txt",
        fx_lines("CAD", "2025-08-07 50.00 - - SAMPLE BANK ADJUSTMENT 99973"),
    )

    fx_ledger.derive()

    rows = fx_ledger.conn.execute(
        select(txn.c.posting_date, txn.c.currency, txn.c.transfer_state)
    )
    if paired:
        assert sorted(tuple(row) for row in rows) == [
            (sent, "CAD", "paired"), (adjusted, "CAD", "paired"),
        ]  # fmt: skip
        assert len(edges(fx_ledger)) == 1
    else:
        assert sorted(tuple(row) for row in rows) == [
            (sent, "USD", None), (adjusted, "CAD", "unresolved"),
        ]  # fmt: skip
        assert edges(fx_ledger) == []


@pytest.mark.parametrize(
    ("pattern", "paired"),
    [("sample dental clinic", True), ("patient receipt", False)],
    ids=["payment-line", "receipt-only"],
)
def test_reversal_wording_on_an_evidence_source_alone_makes_no_reversal(
    chequing: Ledger, pattern: str, paired: bool
) -> None:
    # A receipt is evidence of the payment line, not a line of it: wording only the
    # receipt carries never makes the payment a reversal of the earlier deposit.
    chequing.manual(TRANSFER_HINTS_FILE, f"{pattern},reversal,")
    chequing.file(RECEIPT, debit_only_receipt())
    received, paid = D(2026, 3, 10), D(2026, 3, 15)
    derive(
        chequing,
        line(received, "DEPOSIT   SAMPLE PAYER   99909", Decimal("45.00")),
        line(paid, CLINIC, Decimal("-45.00")),
    )

    # The control: the receipt is the payment's evidence source.
    evidence = chequing.conn.scalar(
        select(func.count()).where(txn_source.c.role == "evidence")
    )
    assert evidence == 1
    state, kind = ("paired", "reversal") if paired else (None, None)
    assert states(chequing) == [
        (received, Decimal("45.00"), state, kind),
        (paid, Decimal("-45.00"), state, kind),
    ]


CU = "Nelson & District Credit Union"


def test_the_credit_union_outgoing_reversal_fixture_nets_through_derive(
    ledger: Ledger,
) -> None:
    # The credit-union parser's own synthetic statement: an e-Transfer debit, the
    # bank's reversal by customer cancel with no payee printed, the debit again.
    ledger.accounts(
        ("cu-chequing", CU, "chequing", "999000000101"),
        ("cu-shares", CU, "savings", "999000000102"),
    )
    ledger.file(
        "nelson-district-cu/2025-11-30.pdf",
        (FIXTURES / "ndcu_statement_current.txt").read_text(encoding="utf-8"),
    )

    ledger.derive()

    sent, cancelled, again = D(2025, 11, 4), D(2025, 11, 9), D(2025, 11, 10)
    assert edges(ledger) == [
        (cancelled, Decimal("1234.56"), sent, Decimal("-1234.56"), True),
    ]
    found = states(ledger)
    assert (sent, Decimal("-1234.56"), "paired", "reversal") in found
    assert (cancelled, Decimal("1234.56"), "paired", "reversal") in found
    assert (again, Decimal("-1234.56"), None, None) in found
    # Nothing else on the statement is touched.
    assert [row for row in found if row[2] is not None] == [
        (sent, Decimal("-1234.56"), "paired", "reversal"),
        (cancelled, Decimal("1234.56"), "paired", "reversal"),
    ]


def card_document(header: str, *lines: str, period: str = "") -> str:
    """A document in the synthetic card layout (``support.parse_card``), opening at
    zero; each line is ``"<posting> <txn date|-> <signed amount> <description>"``."""
    rows = [header, f"ACCOUNT {CARD_IDENTIFIER}"]
    if header == CARD_STATEMENT:
        closing = sum((Decimal(line.split(" ")[2]) for line in lines), Decimal(0))
        rows += [f"PERIOD {period}", "OPENING 0", f"CLOSING {closing}"]
    return "\n".join([*rows, *(f"LINE {line}" for line in lines)]) + "\n"


@pytest.mark.parametrize("with_row", [True, False])
@pytest.mark.parametrize(
    "on_primary", [False, True], ids=["on-additional", "on-primary"]
)
def test_a_reversal_row_matches_either_source_line_alone(
    ledger: Ledger, card_parsers: None, with_row: bool, on_primary: bool
) -> None:
    # The statement prints the credit one way and the export another; the overlap
    # joins them into one txn whose wording is the statement's (the primary source).
    # Only one of the two carries what the row names, the primary or the additional
    # line: the result is the same either way, so it never depends on the order the
    # sources are read in.
    if with_row:
        ledger.manual(TRANSFER_HINTS_FILE, "sample bank adjustment,reversal,")
    ledger.card()
    bought, adjusted = D(2025, 1, 10), D(2025, 1, 14)
    plain, named = "SAMPLE CREDIT 99974", "SAMPLE BANK ADJUSTMENT 99973"
    statement, export_wording = (named, plain) if on_primary else (plain, named)
    ledger.file(
        "synthetic-card/2025-02-05.pdf",
        card_document(
            CARD_STATEMENT,
            "2025-01-10 - -64.00 SAMPLE SHOP 99972",
            f"2025-01-14 - 64.00 {statement}",
            period="2025-01-06 2025-02-05",
        ),
    )
    ledger.file(
        "synthetic-card/2025-02-05-export.csv",
        card_document(
            CARD_TRANSACTIONS,
            "2025-01-10 - -64.00 SAMPLE SHOP 99972",
            f"2025-01-14 - 64.00 {export_wording}",
        ),
    )

    out = ledger.derive()

    # The control: one txn per purchase and credit, each with its export line as an
    # additional source, and the credit's own wording is the statement's.
    assert (out.lines.txns, out.lines.additional_sources) == (2, 2)
    wording = ledger.conn.scalar(select(txn.c.description_norm).where(txn.c.amount > 0))
    assert ("adjustment" in (wording or "")) is on_primary
    state, kind = ("paired", "reversal") if with_row else (None, None)
    assert states(ledger) == [
        (bought, Decimal("-64.00"), state, kind),
        (adjusted, Decimal("64.00"), state, kind),
    ]


# --- stage 8 never reads a counterparty -----------------------------------------------

NAMED: tuple[str, ...] = ("counterpart", "payee", "party", "alias")


def test_stage_8_sends_no_statement_naming_a_counterparty(
    chequing: Ledger, monkeypatch: pytest.MonkeyPatch
) -> None:
    sent: list[str] = []
    running = False

    def record(*args: Any) -> None:
        if running:
            sent.append(str(args[2]).lower())

    pair = stage8.pair_reversals

    def watched(*args: Any, **kwargs: Any) -> None:
        nonlocal running
        running = True
        try:
            pair(*args, **kwargs)
        finally:
            running = False

    monkeypatch.setattr(stage8, "pair_reversals", watched)
    event.listen(chequing.conn, "before_cursor_execute", record)
    try:
        derive(
            chequing,
            line(D(2025, 9, 2), OUTGOING.original, Decimal("-875.00")),
            line(D(2025, 9, 8), OUTGOING.reversal, Decimal("875.00")),
        )
    finally:
        event.remove(chequing.conn, "before_cursor_execute", record)

    assert sent, "the control: stage 8 ran and sent statements"
    assert edges(chequing) != []
    assert [s for s in sent if any(word in s for word in NAMED)] == []


def _words(source: str) -> Iterator[str]:
    """Every name, attribute, argument, import and non-docstring string in
    ``source``."""
    tree = ast.parse(source)
    documented = (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
    docstrings = {
        id(node.body[0].value)
        for node in ast.walk(tree)
        if isinstance(node, documented)
        and node.body
        and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
    }
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            yield node.id
        elif isinstance(node, ast.Attribute):
            yield node.attr
        elif isinstance(node, ast.arg):
            yield node.arg
        elif isinstance(node, ast.keyword) and node.arg:
            yield node.arg
        elif isinstance(node, ast.ImportFrom):
            yield node.module or ""
            yield from (each.asname or each.name for each in node.names)
        elif isinstance(node, ast.Import):
            yield from (each.asname or each.name for each in node.names)
        elif (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in docstrings
        ):
            yield node.value


def named_reads(source: str) -> list[str]:
    return sorted(
        {w for w in _words(source) if any(each in w.lower() for each in NAMED)}
    )


def test_stage_8_source_names_no_counterparty_payee_party_or_alias() -> None:
    # Stage 8 and the line read it shares with stage 9.
    assert named_reads(inspect.getsource(stage8)) == []
    assert named_reads(inspect.getsource(pairing)) == []
    # The control: a planted read is found, by attribute, string and import.
    planted = (
        '"""Mentions counterparty only here."""\n'
        "x = txn.c.counterparty\n"
        'y = txn.c["party_id"]\n'
        "from rheo_finance.features import payees\n"
    )
    assert named_reads(planted) == ["counterparty", "party_id", "payees"]
