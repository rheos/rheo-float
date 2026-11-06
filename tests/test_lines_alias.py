"""Public CLI and operation seams using invented names and injected terminal streams."""

from __future__ import annotations

import ast
import csv
import dataclasses
import datetime as dt
import hashlib
import io
import uuid
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import create_engine, event, insert, select
from sqlalchemy.pool import NullPool

from conftest import DatabaseUnderTest
from rheo_finance import alias, catalogue, composition, operations
from rheo_finance.alias import DraftOut, draft_names
from rheo_finance.alias_store import AliasFacts, AliasLine
from rheo_finance.cli.main import main
from rheo_finance.clock import FakeClock
from rheo_finance.context import FinanceContext, FinancePaths
from rheo_finance.contracts import Role, SafetyClass, field_tiers
from rheo_finance.settings import parse_settings
from rheo_finance.storage.ids import derived_id
from rheo_finance.storage.schema import account, metadata, payee, txn, txn_source
from rheo_finance.tier import Tier

DAY = dt.date(2028, 3, 4)
NAMES = ("Invented Alder", "Sample Willow, Jr.")


def key(label: str) -> uuid.UUID:
    return derived_id(DAY, ("synthetic_lines_alias", label))


@pytest.fixture(scope="module")
def seeded(scratch_database: DatabaseUnderTest) -> Iterator[DatabaseUnderTest]:
    engine = create_engine(scratch_database.owner_url, poolclass=NullPool)
    try:
        with engine.begin() as conn:
            for slug in ("sample-a", "sample-b"):
                conn.execute(
                    insert(account).values(
                        id=key(slug),
                        slug=slug,
                        label=slug.title(),
                        institution="Sample",
                        kind="chequing",
                        currencies=["CAD", "BTC"],
                        cadence="monthly",
                        sourced_by="documents",
                    )
                )
            conn.execute(
                insert(payee).values(
                    id=key("payee"),
                    slug="sample-mapped",
                    display_label="Registered payee",
                    kind="person",
                    patterns=["Mapped Cedar"],
                )
            )
            descriptions = (
                f"EMT Transfer - Debit Payee: {NAMES[0]}",
                f"Interac e-Transfer from {NAMES[1]}",
                "INTERAC E-TRANSFER FROM INVENTED   ALDER",
                "Interac e-Transfer from MAPPED    CEDAR",
                "Grocery purchase",
            )
            for n, description in enumerate(descriptions):
                conn.execute(
                    insert(txn).values(
                        id=key(str(n)),
                        account_id=key("sample-b" if n == 2 else "sample-a"),
                        posting_date=DAY + dt.timedelta(days=n),
                        txn_date=DAY,
                        amount=Decimal("-9.999999999") if n == 0 else Decimal("1.25"),
                        currency="BTC" if n == 0 else "CAD",
                        cad_amount=None,
                        basis="statement",
                        origin="document_line",
                        movement_kind="fiat",
                        description_norm=description,
                        counterparty="Hidden Counterparty",
                        merchant_key="Hidden Merchant",
                        txid="hidden-transaction-id",
                        from_address="hidden-wallet-address",
                        display_label=(
                            "e-Transfer (name withheld)"
                            if n < 3
                            else "Registered payee"
                            if n == 3
                            else "Groceries"
                        ),
                        transfer_state="unresolved" if n == 0 else None,
                        pair_kind="reversal" if n == 1 else None,
                        allocation_state="unallocated" if n == 2 else None,
                        exclusion="spam" if n == 3 else None,
                        provisional=(n == 1),
                    )
                )
                # Two sources must still yield just one canonical line reference.
                for role in ("primary", "additional"):
                    conn.execute(
                        insert(txn_source).values(
                            txn_id=key(str(n)),
                            manual_ref=f"synthetic-{n}-{role}",
                            line_key_id=key(f"{n}-{role}"),
                            role=role,
                        )
                    )
        yield scratch_database
    finally:
        engine.dispose()


@pytest.fixture
def ctx(seeded: DatabaseUnderTest, tmp_path: Path) -> FinanceContext:
    catalogue.load_all()
    context = FinanceContext(
        settings=parse_settings(""),
        clock=FakeClock(dt.datetime(2028, 3, 4, tzinfo=dt.UTC)),
        paths=FinancePaths(data=tmp_path),
        role=Role.OWNER,
        snapshot_provider=lambda _uow, _ref: None,
    )
    return operations.with_databases(
        context, owner_url=seeded.owner_url, reader_url=seeded.reader_url
    )


class Terminal(io.StringIO):
    def isatty(self) -> bool:
        return True


def terminal(monkeypatch: pytest.MonkeyPatch) -> tuple[Terminal, Terminal]:
    stdin, stdout = Terminal(), Terminal()
    monkeypatch.setattr("sys.stdin", stdin)
    monkeypatch.setattr("sys.stdout", stdout)
    return stdin, stdout


def test_lines_reader_connection_precision_cardinality_and_privacy(
    ctx: FinanceContext,
    seeded: DatabaseUnderTest,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    reader_only = operations.with_databases(
        ctx, owner_url=seeded.owner_url.set(port=1), reader_url=seeded.reader_url
    )
    monkeypatch.setattr(composition, "build_context", lambda _role: reader_only)
    assert main(["lines"]) == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    shown = captured.out
    lines = shown.splitlines()
    assert lines[:2] == ["finance lines: 5 lines", ""]
    assert len(lines[2:]) == 5
    assert lines[2] == (
        f"finance.txn:{key('0-primary')} | Sample-A | 2028-03-04 | -9.999999999 BTC"
        " | e-Transfer (name withheld) | transfer=unresolved"
    )
    assert "pair=reversal; provisional" in shown
    assert "allocation=unallocated" in shown and "exclusion=spam" in shown
    folded = "".join(shown.casefold().split())
    for secret in (
        *NAMES,
        "Mapped Cedar",
        "Hidden Counterparty",
        "Hidden Merchant",
        "hidden-transaction-id",
        "hidden-wallet-address",
    ):
        assert "".join(secret.casefold().split()) not in folded


@pytest.mark.parametrize(
    ("flags", "count"),
    [
        (["--account", "sample-a"], 4),
        (["--from", "2028-03-05", "--to", "2028-03-06"], 2),
        (["--state", "unresolved"], 1),
        (["--state", "reversal"], 1),
        (["--state", "unallocated"], 1),
        (["--state", "spam"], 1),
        (
            [
                "--account",
                "sample-b",
                "--from",
                "2028-03-06",
                "--to",
                "2028-03-06",
                "--state",
                "unallocated",
            ],
            1,
        ),
        (["--account", "missing"], 0),
    ],
)
def test_lines_flags_reach_reader_operation(
    ctx: FinanceContext,
    flags: list[str],
    count: int,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(composition, "build_context", lambda _role: ctx)
    assert main(["lines", *flags]) == 0
    shown = capsys.readouterr()
    assert shown.err == ""
    assert shown.out.splitlines()[0] == f"finance lines: {count} lines"
    assert shown.out.count("finance.txn:") == count
    if count == 0:
        assert "no lines" in shown.out


@pytest.mark.parametrize(
    "flags", [["--from", "invalid"], ["--from", "2028-03-07", "--to", "2028-03-04"]]
)
def test_lines_invalid_dates_refuse_without_echoing_input(
    ctx: FinanceContext,
    flags: list[str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(composition, "build_context", lambda _role: ctx)
    assert main(["lines", *flags]) == 1
    shown = capsys.readouterr()
    assert shown.out == "" and "refused" in shown.err


def test_alias_operation_contract_and_restricted_output(ctx: FinanceContext) -> None:
    spec = catalogue.load_all()["finance.alias.draft"]
    assert spec.safety_class is SafetyClass.DRAFT and spec.connection == "owner"
    assert spec.owner_only and spec.roles == {Role.OWNER} and spec.tool_name is None
    assert field_tiers(DraftOut) == {"names": Tier.RESTRICTED}
    out = operations.dispatch("finance.alias.draft", {}, Role.OWNER, ctx=ctx)
    assert isinstance(out, DraftOut)
    assert out.names == [name.casefold() for name in NAMES]


def test_alias_policy_accepts_typed_repository_facts_without_a_database() -> None:
    class MemoryRepository:
        def facts(self) -> AliasFacts:
            return AliasFacts(
                patterns=("Mapped Cedar",),
                lines=tuple(
                    AliasLine(description, Decimal("1.25"), "chequing", "Sample")
                    for description in (
                        f"EMT Transfer - Debit Payee: {NAMES[0]}",
                        f"Interac e-Transfer from {NAMES[1]}",
                        "Interac e-Transfer from INVENTED   ALDER",
                        "Interac e-Transfer from MAPPED CEDAR",
                        "Grocery purchase",
                    )
                ),
            )

    assert draft_names(MemoryRepository()).names == [name.casefold() for name in NAMES]


def test_alias_policy_has_no_direct_sql_or_schema_dependency() -> None:
    assert alias.__file__ is not None
    tree = ast.parse(Path(alias.__file__).read_text())
    imports = [
        name
        for node in ast.walk(tree)
        for name in (
            [item.name for item in node.names]
            if isinstance(node, ast.Import)
            else [node.module or ""]
            if isinstance(node, ast.ImportFrom)
            else []
        )
    ]
    forbidden = ("sqlalchemy", "rheo_finance.storage", "rheo_finance.db")
    assert not [name for name in imports if name.startswith(forbidden)]


@pytest.mark.parametrize("role", [Role.MEMBER, Role.SERVICE])
def test_alias_refuses_non_owner_roles(ctx: FinanceContext, role: Role) -> None:
    with pytest.raises(operations.RoleRefused):
        operations.dispatch(
            "finance.alias.draft", {}, role, ctx=dataclasses.replace(ctx, role=role)
        )


def test_alias_refuses_actual_reader_connection_even_as_owner(
    ctx: FinanceContext,
    seeded: DatabaseUnderTest,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    reader = operations.with_databases(
        ctx, owner_url=seeded.reader_url, reader_url=seeded.reader_url
    )
    with pytest.raises(operations.RoleRefused, match="reader connection"):
        operations.dispatch("finance.alias.draft", {}, Role.OWNER, ctx=reader)
    monkeypatch.setattr(composition, "build_context", lambda _role: reader)
    _, stdout = terminal(monkeypatch)
    assert main(["alias", "draft"]) == 1
    assert stdout.getvalue() == ""
    assert "refuses the reader connection" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("stdin_tty", "stdout_tty"), [(False, False), (False, True), (True, False)]
)
def test_alias_refuses_either_nonterminal_before_building_context(
    monkeypatch: pytest.MonkeyPatch,
    stdin_tty: bool,
    stdout_tty: bool,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr("sys.stdin", Terminal() if stdin_tty else io.StringIO())
    stdout = Terminal() if stdout_tty else io.StringIO()
    monkeypatch.setattr("sys.stdout", stdout)

    def forbidden(_role: Role) -> FinanceContext:
        raise AssertionError("nonterminal must refuse before context creation")

    monkeypatch.setattr(composition, "build_context", forbidden)
    assert main(["alias", "draft"]) == 1
    assert stdout.getvalue() == ""
    assert "stdin and stdout must be terminals" in capsys.readouterr().err


def test_alias_terminal_csv_drafts_and_no_writes(
    ctx: FinanceContext,
    seeded: DatabaseUnderTest,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = create_engine(seeded.owner_url, poolclass=NullPool)

    def snapshot() -> str:
        with engine.connect() as conn:
            rows = [
                (table.name, sorted(map(repr, conn.execute(select(table)).all())))
                for table in metadata.sorted_tables
            ]
        return hashlib.sha256(repr(rows).encode()).hexdigest()

    def files() -> dict[str, bytes]:
        return {
            str(path.relative_to(ctx.paths.data)): path.read_bytes()
            for path in ctx.paths.data.rglob("*")
            if path.is_file()
        }

    statements: list[str] = []

    def capture_statement(
        _conn: object,
        _cursor: object,
        statement: str,
        _params: object,
        _context: object,
        _many: object,
    ) -> None:
        statements.append(statement)

    before_db, before_files = snapshot(), files()
    monkeypatch.setattr(composition, "build_context", lambda _role: ctx)
    _, stdout = terminal(monkeypatch)
    event.listen(engine, "before_cursor_execute", capture_statement)
    # Capture dispatch's separate connection too, without changing its public seam.
    from sqlalchemy.engine import Engine

    event.listen(Engine, "before_cursor_execute", capture_statement)
    try:
        assert main(["alias", "draft"]) == 0
    finally:
        event.remove(Engine, "before_cursor_execute", capture_statement)
        event.remove(engine, "before_cursor_execute", capture_statement)
    assert statements and all(
        s.lstrip().upper().startswith("SELECT") for s in statements
    )
    assert snapshot() == before_db and files() == before_files
    engine.dispose()
    shown = stdout.getvalue()
    assert (
        shown.splitlines()[0]
        == "finance alias draft: 2 unmatched names; nothing written."
    )
    aliases, payees = shown.split("party_aliases.csv\n", 1)[1].split("payees.csv\n")
    assert list(csv.reader(io.StringIO(aliases))) == [
        ["party", "kind", "alias"],
        ["", "payer_name", NAMES[0].casefold()],
        ["", "payer_name", NAMES[1].casefold()],
    ]
    assert list(csv.reader(io.StringIO(payees))) == [
        ["slug", "label", "kind", "patterns", "party", "paid_from"],
        ["", "", "person", NAMES[0].casefold(), "", ""],
        ["", "", "person", NAMES[1].casefold(), "", ""],
    ]
    assert "mapped cedar" not in shown.casefold() and "Hidden Counterparty" not in shown
