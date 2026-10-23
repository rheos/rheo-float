"""The local AC-7 parity check: the ledger against the existing parsers' outputs (FR-7).

``scripts/parity_check.py`` runs it. It reads the ledger's per-statement totals through
``finance.statement.list`` (read only) and the existing monitors' CSVs, ``vancity.csv``
and ``virgin-plus.csv``, from a directory it is given, and prints statement dates and
a result word per statement, never an amount, an identifier or a name.

The parity rule, as ``compare`` applies it:

- **A statement the existing parser accepts** must have equal ledger per-statement
  totals: the ledger parsed it, and its totals equal the parser's.
- **A file the existing parser rejects** must be rejected by the ledger too: the ledger
  holds no parsed document for that statement (a rejected one, or none it could date).
- **Where the ledger is stricter or laxer than ``docextract.validate`` for a defined
  reason** (below), the result names that reason and still FAILS: AC-7 is binary, so
  a bill the existing parser accepts and the ledger rejects, or one it rejects and the
  ledger accepts, is a parity failure whatever the cause (a Conductor decision of the
  build run, 2026-10-23). The named kinds only say why.

What "accepts" means, per source:

- **Vancity** (``vancity.csv``, one row per account section that moved or held money):
  ``checks_ok`` true on every row of the statement. The ledger runs the same
  ``docextract.vancity`` checks and rejects the whole document (``checks_failed``) when
  any fails. A statement is keyed by its ``period_end``. Its totals are the multiset of
  (opening, withdrawals, deposits, closing) over its rows; the ledger's are (opening,
  total debits, total credits, closing) over its statement periods. On BOTH sides a
  section whose four values are all zero is left out (one predicate, ``_holds_money``):
  the monitor skips a section with no opening, no closing and no transactions, but
  writes one whose transactions are notices only (zeros all through), and the ledger
  keeps a period for every section, so an all-zero section says nothing either way.
- **Virgin** (``virgin-plus.csv``, one row per bill): the existing parser is
  ``docextract.reference`` plus what ``docextract.validate`` flags on its fields. The
  monitor runs ``validate`` on the model's reading only; this check runs the real
  ``validate`` (through ``parsers.docextract_adapter.virgin_flags``, no copy of its
  rules here) on the reference fields as the CSV holds them, with ``usage_charges``
  empty (the CSV holds only their sum), so a disagreement with the ledger has a named
  cause:

  * ``source`` other than ``reference``: the reference parser could not read the bill,
    so the existing parser rejects it;
  * the identity: ``validate`` flags unpaid balance plus current charges against the
    total due (its ``total_due`` ``mismatch`` flag) only when they differ by more than
    one cent. The ledger's ``bill_identity`` check (``parsers.virgin_bill``) is exact.
    A difference over a cent: both reject. A difference of exactly one cent:
    ``validate`` accepts and the ledger rejects with ``identity_mismatch``, the failing
    kind ``FAIL_STRICTER_ONE_CENT``;
  * any other ``validate`` flag (a missing field, a negative amount, a positive
    adjustment, a due date outside its window: whatever the pinned docextract
    flags), none of which the ledger checks. ``validate`` rejects such a bill; when the
    ledger rejects it too, both reject; when the ledger accepts it, the totals are
    still compared, and the kind is ``FAIL_TOTALS`` when they differ, else the failing
    kind ``FAIL_LAXER_VALIDATE_FLAG``.

  A bill is keyed by its bill month (the ledger's period is the bill-date month and
  stores no bill date), its total is the total due against the ledger's balance due.

A statement only the ledger holds is ``LEDGER_ONLY``, the one kind that is neither pass
nor fail (the monitors never saw it, so AC-7 says nothing of it). Two documents on
either side for one statement are ``FAIL_DUPLICATE``. Totals are compared exactly, to
the cent: one cent apart is a failure.
"""

from __future__ import annotations

import csv
import datetime as dt
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Final, Literal

from rheo_finance.parsers.docextract_adapter import virgin_flags
from rheo_finance.render import Report

if TYPE_CHECKING:
    from rheo_finance.derive.ops import StatementListOut

Source = Literal["vancity", "virgin"]
Totals = tuple[Decimal | None, ...]
# The existing parser's verdict on one statement.
Verdict = Literal["accepted", "rejected", "one_cent", "validate_flag"]

VANCITY_FILE: Final = "vancity.csv"
VIRGIN_FILE: Final = "virgin-plus.csv"
VANCITY_PARSER: Final = "vancity.statement"
VIRGIN_PARSER: Final = "virgin_plus.bill"
SOURCES: Final[dict[str, Source]] = {VANCITY_PARSER: "vancity", VIRGIN_PARSER: "virgin"}
CENT: Final = Decimal("0.01")
IDENTITY_MISMATCH: Final = "identity_mismatch"
# validate's flag for the identity off by more than a cent.
VALIDATE_IDENTITY: Final = ("total_due", "mismatch")
# The fields validate reads, as the CSV holds them (usage_charges goes in empty).
VALIDATE_FIELDS: Final = (
    "bill_date", "due_date", "total_due", "unpaid_balance", "current_charges",
    "late_payment_charge", "device_payment", "adjustments", "data_used_mb",
)  # fmt: skip

VANCITY_COLUMNS: Final = ("period_end", "opening", "withdrawals", "deposits", "closing",
                          "checks_ok")  # fmt: skip
VIRGIN_COLUMNS: Final = (
    "bill_date", "due_date", "total_due", "unpaid_balance", "current_charges",
    "late_payment_charge", "device_payment", "adjustments", "data_used_mb", "source",
)  # fmt: skip


class ParityInputError(Exception):
    """A reference file cannot be read, or the comparison cannot run. Messages name
    the file, row and column, or an exception type, never a cell's value."""


class Kind(Enum):
    PASS_EQUAL = ("pass", "totals equal")
    PASS_BOTH_REJECT = ("pass", "both reject it")
    FAIL_STRICTER_ONE_CENT = (
        "fail",
        "the ledger rejects a bill docextract.validate passes within one cent",
    )
    FAIL_LAXER_VALIDATE_FLAG = (
        "fail",
        "the ledger accepts a bill docextract.validate flags, totals equal",
    )
    LEDGER_ONLY = ("ledger only", "not in the existing parser's output")
    FAIL_TOTALS = ("fail", "totals differ")
    FAIL_LEDGER_REJECTS = (
        "fail",
        "the existing parser accepts it, the ledger does not",
    )
    FAIL_LEDGER_ACCEPTS = (
        "fail",
        "the existing parser rejects it, the ledger accepts it",
    )
    FAIL_NOT_IN_LEDGER = ("fail", "the existing parser accepts it, the ledger lacks it")
    FAIL_DUPLICATE = ("fail", "more than one document for this statement")

    @property
    def group(self) -> str:
        return self.value[0]

    @property
    def words(self) -> str:
        return self.value[1]

    @property
    def failed(self) -> bool:
        return self.group == "fail"


@dataclass(frozen=True)
class LedgerStatement:
    source: Source
    key: dt.date
    # What to print: a Vancity period end, a Virgin bill month.
    label: str
    accepted: bool
    reject_reason: str | None
    totals: tuple[Totals, ...]


@dataclass(frozen=True)
class ReferenceStatement:
    source: Source
    key: dt.date
    # The statement date: a Vancity period end, a Virgin bill date.
    label: str
    verdict: Verdict
    totals: tuple[Totals, ...]


@dataclass(frozen=True)
class Result:
    source: Source
    label: str
    kind: Kind


# --- the comparison -------------------------------------------------------------------


def _same(left: Iterable[Totals], right: Iterable[Totals]) -> bool:
    return Counter(left) == Counter(right)


def _one(reference: ReferenceStatement, ledger: LedgerStatement | None) -> Kind:
    accepts = ledger is not None and ledger.accepted
    if reference.verdict == "rejected":
        return Kind.FAIL_LEDGER_ACCEPTS if accepts else Kind.PASS_BOTH_REJECT
    if reference.verdict == "validate_flag":
        if ledger is None or not ledger.accepted:
            return Kind.PASS_BOTH_REJECT
        if _same(reference.totals, ledger.totals):
            return Kind.FAIL_LAXER_VALIDATE_FLAG
        return Kind.FAIL_TOTALS
    if ledger is None:
        return Kind.FAIL_NOT_IN_LEDGER
    if not ledger.accepted:
        if (
            reference.verdict == "one_cent"
            and ledger.reject_reason == IDENTITY_MISMATCH
        ):
            return Kind.FAIL_STRICTER_ONE_CENT
        return Kind.FAIL_LEDGER_REJECTS
    if _same(reference.totals, ledger.totals):
        return Kind.PASS_EQUAL
    return Kind.FAIL_TOTALS


def compare(
    ledger_rows: Sequence[LedgerStatement],
    reference_rows: Sequence[ReferenceStatement],
) -> list[Result]:
    """One result per statement either side holds, by source and date.

    See the module docstring for the rule and for what "accepts" and "rejects" mean
    for each source: the existing parser accepts a Vancity statement when
    ``checks_ok`` is true, and a Virgin bill when the reference parser read it and
    ``docextract.validate`` would raise no flag on its fields. The ledger accepts a
    statement when its document is ``parsed``.
    """
    ledger: dict[tuple[Source, dt.date], list[LedgerStatement]] = defaultdict(list)
    for row in ledger_rows:
        ledger[(row.source, row.key)].append(row)
    reference: dict[tuple[Source, dt.date], list[ReferenceStatement]] = defaultdict(
        list
    )
    for ref in reference_rows:
        reference[(ref.source, ref.key)].append(ref)

    results: list[Result] = []
    for key, refs in reference.items():
        found = ledger.get(key, [])
        if len(refs) > 1 or len(found) > 1:
            kind = Kind.FAIL_DUPLICATE
        else:
            kind = _one(refs[0], found[0] if found else None)
        results.append(Result(refs[0].source, refs[0].label, kind))
    for key, rows in ledger.items():
        if key not in reference:
            results.append(Result(rows[0].source, rows[0].label, Kind.LEDGER_ONLY))
    return sorted(results, key=lambda r: (r.source, r.label, r.kind.name))


# --- the ledger side ------------------------------------------------------------------


def _holds_money(totals: Totals) -> bool:
    """Whether a Vancity section's (opening, withdrawals, deposits, closing) holds
    any non-zero value. Both sides leave out the sections that do not."""
    return any(value is not None and value != 0 for value in totals)


def _vancity_totals(out_periods: Iterable[Totals]) -> tuple[Totals, ...]:
    return tuple(each for each in out_periods if _holds_money(each))


def ledger_statements(out: StatementListOut) -> list[LedgerStatement]:
    """``finance.statement.list`` output as comparable rows. A document with no
    period (no parser could date it) cannot be keyed and is left out."""
    rows: list[LedgerStatement] = []
    for doc in out.documents:
        source = SOURCES.get(doc.parser_id)
        if source is None or doc.period_start is None or doc.period_end is None:
            continue
        if source == "vancity":
            key, label = doc.period_end, doc.period_end.isoformat()
            totals = _vancity_totals(
                (p.opening, p.total_debits, p.total_credits, p.closing)
                for p in doc.periods
            )
        else:
            key, label = doc.period_start, f"{doc.period_start:%Y-%m}"
            totals = tuple((p.balance_due,) for p in doc.periods)
        rows.append(
            LedgerStatement(
                source=source,
                key=key,
                label=label,
                accepted=doc.status == "parsed",
                reject_reason=doc.reject_reason,
                totals=totals,
            )
        )
    return rows


# --- the reference side ---------------------------------------------------------------


def _rows(path: Path, columns: Sequence[str]) -> list[dict[str, str]]:
    reader: csv.DictReader[str] | None = None
    try:
        with path.open(encoding="utf-8", newline="") as handle:
            reader = csv.DictReader(handle)
            missing = [c for c in columns if c not in (reader.fieldnames or ())]
            if missing:
                raise ParityInputError(f"{path.name}: no column {', '.join(missing)}")
            return [dict(row) for row in reader]
    except csv.Error:
        # csv's own messages can quote the line; name the row only.
        line = reader.line_num if reader is not None else 0
        raise ParityInputError(f"{path.name}: row {line}: not valid CSV") from None
    except UnicodeDecodeError:
        raise ParityInputError(f"{path.name}: not UTF-8 text") from None
    except OSError as exc:
        raise ParityInputError(
            f"{path.name}: cannot be read ({exc.strerror})"
        ) from None


def _money(
    path: Path, line: int, column: str, row: Mapping[str, str]
) -> Decimal | None:
    cell = (row.get(column) or "").strip()
    if not cell:
        return None
    try:
        value = Decimal(cell)
    except InvalidOperation:
        raise ParityInputError(
            f"{path.name}: row {line}: column {column} is not an amount"
        ) from None
    if not value.is_finite():
        raise ParityInputError(
            f"{path.name}: row {line}: column {column} is not finite"
        )
    return value.quantize(CENT)


def _date(path: Path, line: int, column: str, row: Mapping[str, str]) -> dt.date | None:
    cell = (row.get(column) or "").strip()
    if not cell:
        return None
    try:
        return dt.date.fromisoformat(cell)
    except ValueError:
        raise ParityInputError(
            f"{path.name}: row {line}: column {column} is not an ISO date"
        ) from None


def _flag(path: Path, line: int, column: str, row: Mapping[str, str]) -> bool:
    cell = (row.get(column) or "").strip().lower()
    if cell not in ("true", "false"):
        raise ParityInputError(
            f"{path.name}: row {line}: column {column} is not true or false"
        )
    return cell == "true"


def read_vancity(path: Path) -> list[ReferenceStatement]:
    """``vancity.csv`` as one statement per ``period_end``."""
    statements: dict[dt.date, list[tuple[Totals, bool]]] = defaultdict(list)
    for line, row in enumerate(_rows(path, VANCITY_COLUMNS), start=2):
        end = _date(path, line, "period_end", row)
        if end is None:
            raise ParityInputError(f"{path.name}: row {line}: no period_end")
        totals = tuple(
            _money(path, line, column, row)
            for column in ("opening", "withdrawals", "deposits", "closing")
        )
        ok = _flag(path, line, "checks_ok", row)
        # An all-zero row adds no totals but its checks_ok still counts.
        statements[end].append((totals, ok))
    return [
        ReferenceStatement(
            source="vancity",
            key=end,
            label=end.isoformat(),
            verdict="accepted" if all(ok for _, ok in rows) else "rejected",
            totals=tuple(totals for totals, _ in rows if _holds_money(totals)),
        )
        for end, rows in statements.items()
    ]


def virgin_verdict(fields: Mapping[str, object]) -> Verdict:
    """The existing parser's verdict on one Virgin bill (see the module docstring).

    ``fields`` holds the CSV's columns: dates as ``date``, amounts as ``Decimal``,
    ``source`` as text, an empty cell as None. The flags are ``docextract.validate``'s
    own; only the exact identity gap, the ledger's rule, is computed here.
    """
    if fields.get("source") != "reference":
        return "rejected"
    flags = virgin_flags(
        {name: fields.get(name) for name in VALIDATE_FIELDS} | {"usage_charges": []}
    )
    if VALIDATE_IDENTITY in flags:
        return "rejected"
    if flags:
        return "validate_flag"
    unpaid, current, total = (
        fields.get(name) for name in ("unpaid_balance", "current_charges", "total_due")
    )
    if (
        isinstance(unpaid, Decimal)
        and isinstance(current, Decimal)
        and isinstance(total, Decimal)
        and unpaid + current != total
    ):
        return "one_cent"
    return "accepted"


def read_virgin(path: Path) -> list[ReferenceStatement]:
    """``virgin-plus.csv`` as one statement per bill, keyed by bill month."""
    statements: list[ReferenceStatement] = []
    for line, row in enumerate(_rows(path, VIRGIN_COLUMNS), start=2):
        bill_date = _date(path, line, "bill_date", row)
        if bill_date is None:
            raise ParityInputError(f"{path.name}: row {line}: no bill_date")
        fields: dict[str, object] = {
            "bill_date": bill_date,
            "due_date": _date(path, line, "due_date", row),
            "source": (row.get("source") or "").strip(),
        }
        for column in VIRGIN_COLUMNS:
            if column not in fields:
                fields[column] = _money(path, line, column, row)
        total = fields["total_due"]
        statements.append(
            ReferenceStatement(
                source="virgin",
                key=bill_date.replace(day=1),
                label=bill_date.isoformat(),
                verdict=virgin_verdict(fields),
                totals=((total if isinstance(total, Decimal) else None,),),
            )
        )
    return statements


def read_reference(directory: Path) -> list[ReferenceStatement]:
    return [
        *read_vancity(directory / VANCITY_FILE),
        *read_virgin(directory / VIRGIN_FILE),
    ]


# --- the report -----------------------------------------------------------------------


def report(results: Sequence[Result]) -> Report:
    """Counts, then one line per statement: its date, source and result words."""
    counts = Counter(each.kind.group for each in results)
    kinds = Counter(each.kind for each in results)
    headline = [
        f"parity: {len(results)} statements, {counts['pass']} pass,"
        f" {counts['fail']} fail",
        "of the fails, ledger stricter by one cent:"
        f" {kinds[Kind.FAIL_STRICTER_ONE_CENT]},"
        f" ledger laxer than validate: {kinds[Kind.FAIL_LAXER_VALIDATE_FLAG]};"
        f" ledger only: {counts['ledger only']}",
    ]
    detail = [
        f"{each.label} {each.source}: {each.kind.group}, {each.kind.words}"
        for each in results
    ]
    return Report(headline, detail)
