"""The ledger operations: ``finance.ledger.rebuild``, ``finance.ledger.derive``,
``finance.ledger.digest``, and the input fingerprint (FR-5, AC-5, AC-3a).

Seams: the three operations through ``dispatch`` over a synthetic archive and manual
files, read back through the digest, the derived tables and the decisions; and
``fingerprint`` over a fake clock. Every value is synthetic, and a "PDF" is layout text
(the PDF extractor is replaced by a text read for the test).
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import hashlib
import socket
from collections.abc import Iterator
from pathlib import Path
from types import MappingProxyType
from typing import Any, NoReturn

import pytest
from pydantic import BaseModel
from sqlalchemy import Connection, DateTime, func, select, update

from derive.support import (
    CARD_IDENTIFIER,
    CARD_INSTITUTION,
    CARD_ISSUER,
    CARD_PARSERS,
    CSV_IDENTIFIER,
    FIXTURES,
    STATEMENT_IDENTIFIER,
    Ledger,
    MakeContext,
    card_fixture,
    read_as_text,
)
from guards.source_tree import PACKAGE, imports, modules, names_module
from rheo_finance import catalogue
from rheo_finance.clock import FakeClock
from rheo_finance.contracts import Role, SafetyClass
from rheo_finance.derive import run as derive_run
from rheo_finance.derive.digest import LEFT_OUT, digested_tables
from rheo_finance.derive.fingerprint import fingerprint
from rheo_finance.derive.ops import DeriveOut, DigestOut, RebuildOut
from rheo_finance.derive.snapshot import txn_snapshot
from rheo_finance.documents import issuers
from rheo_finance.operations import dispatch
from rheo_finance.parsers import docextract_adapter, registry
from rheo_finance.settings import parse_settings
from rheo_finance.storage import schema
from rheo_finance.storage.ids import uuid7
from rheo_finance.storage.refs import format_ref
from rheo_finance.storage.schema import (
    decision,
    decision_target,
    document,
    sink_spec,
    txn,
    txn_source,
)

CSV_FIXTURE = (FIXTURES / "vancity_chequing_jump.csv").read_text(encoding="utf-8")
# Archive path: fixture file. Statements, exports and downloads that overlap.
CARD_DOCS = {
    "synthetic-card/2024-03-21-export.csv": "card_2024_export.csv",
    "synthetic-card/2024-03-06-download.pdf": "card_2024_download.txt",
    "synthetic-card/2025-10-05.pdf": "card_statement_2025_10.txt",
    "synthetic-card/2025-11-05.pdf": "card_statement_2025_11.txt",
    "synthetic-card/2025-11-15-download.pdf": "card_2025_download.txt",
    "synthetic-card/2025-12-05.pdf": "card_statement_2025_12.txt",
    "synthetic-card/2023-05-09-a.csv": "card_2023_export_a.csv",
    "synthetic-card/2023-05-15-b.csv": "card_2023_export_b.csv",
}
VANCOUVER = 'timezone = "America/Vancouver"'


@pytest.fixture
def ledger(
    owner_conn: Connection,
    rolled_back_context: MakeContext,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Ledger:
    """A ledger whose operations read a "PDF" as text, with the synthetic card issuer
    mapped and its parsers registered, for this test only. Every operation is
    registered (``finance.decision.record`` included)."""
    catalogue.load_all()
    monkeypatch.setattr(docextract_adapter, "pdf_text", read_as_text)
    monkeypatch.setattr(
        issuers,
        "ISSUER_DIRS",
        MappingProxyType(
            {
                **issuers.ISSUER_DIRS,
                CARD_ISSUER: issuers.IssuerDir("statement", {"csv": "export"}),
            }
        ),
    )
    registry.load_parsers()
    for parser in CARD_PARSERS:
        monkeypatch.setitem(registry.PARSERS, parser.parser_id, parser)
    return Ledger(owner_conn, rolled_back_context(Role.OWNER), tmp_path)


def op(ledger: Ledger, name: str, payload: dict[str, Any] | None = None) -> BaseModel:
    return dispatch(name, payload or {}, Role.OWNER, ctx=ledger.ctx)


def rebuild(ledger: Ledger) -> RebuildOut:
    out = op(ledger, "finance.ledger.rebuild")
    assert isinstance(out, RebuildOut)
    return out


def derive(ledger: Ledger) -> DeriveOut:
    out = op(ledger, "finance.ledger.derive")
    assert isinstance(out, DeriveOut)
    return out


def digest(ledger: Ledger) -> str:
    out = op(ledger, "finance.ledger.digest")
    assert isinstance(out, DigestOut)
    return out.digest


def table_counts(ledger: Ledger) -> dict[str, int]:
    out = op(ledger, "finance.ledger.digest")
    assert isinstance(out, DigestOut)
    return {each.name: each.count for each in out.tables}


def statuses(out: DeriveOut) -> dict[str, int]:
    return {each.name: each.count for each in out.documents}


def seed(ledger: Ledger) -> None:
    """Two accounts and documents of every outcome: statements and overlapping
    exports, a repeated copy (superseded), one naming an unknown account (rejected) and
    one no parser recognises (unparsed)."""
    ledger.accounts(
        ("card", CARD_INSTITUTION, "credit_card", CARD_IDENTIFIER),
        ("chequing", "Vancity", "chequing", CSV_IDENTIFIER),
    )
    ledger.add_identifier("chequing", STATEMENT_IDENTIFIER)
    for path, fixture in CARD_DOCS.items():
        ledger.file(path, card_fixture(fixture))
    ledger.file("vancity/export.csv", CSV_FIXTURE)
    ledger.file("vancity/export-copy.csv", CSV_FIXTURE + "\n")
    ledger.file(
        "synthetic-card/2025-10-06-other.pdf",
        card_fixture("card_statement_2025_10.txt").replace(
            CARD_IDENTIFIER, "999000000099"
        ),
    )
    ledger.file("synthetic-card/2025-01-01-notes.csv", "NOT A CARD DOCUMENT\n")


SEEDED = {"parsed": 9, "superseded": 1, "rejected": 1, "unparsed": 1}


# --- rebuild and digest -------------------------------------------------------------


def test_two_rebuilds_on_unchanged_inputs_give_one_digest(ledger: Ledger) -> None:
    seed(ledger)

    first = rebuild(ledger)
    before = digest(ledger)
    second = rebuild(ledger)

    assert statuses(first) == statuses(second) == SEEDED
    assert first.txns > 0 and first.additional_sources > 0
    assert second.parsed_now == sum(SEEDED.values()) and second.kept == 0
    assert digest(ledger) == before
    counts = table_counts(ledger)
    assert counts["txn"] == first.txns
    assert counts["document"] == sum(SEEDED.values())


def test_derive_and_rebuild_give_one_digest_in_every_order(ledger: Ledger) -> None:
    seed(ledger)

    first = derive(ledger)
    after_derive = digest(ledger)
    second = derive(ledger)
    derive_after_derive = digest(ledger)
    rebuild(ledger)
    rebuild_after_derive = digest(ledger)
    third = derive(ledger)
    derive_after_rebuild = digest(ledger)

    assert first.parsed_now == sum(SEEDED.values())
    # Nothing moved: every stored parse is kept, and the ledger is the same.
    assert second.parsed_now == third.parsed_now == 0
    assert second.kept == third.kept == sum(SEEDED.values())
    assert statuses(second) == statuses(third) == SEEDED
    assert (
        after_derive
        == derive_after_derive
        == rebuild_after_derive
        == derive_after_rebuild
    )


def test_derive_parses_only_new_or_changed_documents(
    ledger: Ledger, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed(ledger)
    derive(ledger)
    total = sum(SEEDED.values())

    # A new document: parsed alone.
    ledger.file(
        "synthetic-card/2025-02-01-new.csv",
        "SYNTHETIC CARD TRANSACTIONS\nACCOUNT 999000000033\n"
        "LINE 2025-01-28 - -7.00 SAMPLE CAFE 99961\n",
    )
    out = derive(ledger)
    assert (out.parsed_now, out.kept) == (1, total)
    total += 1

    # A parser's version moves: the documents of its issuer and format are parsed.
    csv_parser = next(p for p in CARD_PARSERS if p.parser_id.endswith("_csv"))
    monkeypatch.setitem(
        registry.PARSERS,
        csv_parser.parser_id,
        dataclasses.replace(csv_parser, version="2"),
    )
    out = derive(ledger)
    card_csvs = (
        sum(1 for path in CARD_DOCS if path.endswith(".csv")) + 2
    )  # the unrecognised one and the new one
    assert (out.parsed_now, out.kept) == (card_csvs, total - card_csvs)

    # An account joins the registry: every document is parsed against it.
    ledger.add_identifier("chequing", "999000000044")
    out = derive(ledger)
    assert (out.parsed_now, out.kept) == (total, 0)

    derived = digest(ledger)
    rebuild(ledger)
    assert digest(ledger) == derived


def test_an_account_that_leaves_the_registry_reparses_its_documents(
    ledger: Ledger,
) -> None:
    seed(ledger)
    derive(ledger)

    ledger.accounts(("chequing", "Vancity", "chequing", CSV_IDENTIFIER))
    ledger.add_identifier("chequing", STATEMENT_IDENTIFIER)
    out = derive(ledger)

    assert out.kept == 0
    # Every card document now names an unregistered account.
    assert statuses(out)["rejected"] == 9
    derived = digest(ledger)
    rebuild(ledger)
    assert digest(ledger) == derived


def test_a_kept_document_that_a_new_copy_supersedes_reads_as_freshly_parsed(
    ledger: Ledger,
) -> None:
    # The first copy's lines become txns, so stage 3 writes their keys. A second
    # copy with a larger sha256 then supersedes it while its parse is kept: its lines
    # must carry no key, as a fresh parse would leave them.
    ledger.accounts(("chequing", "Vancity", "chequing", CSV_IDENTIFIER))
    ledger.file("vancity/export.csv", CSV_FIXTURE)
    derive(ledger)
    first = hashlib.sha256(CSV_FIXTURE.encode("utf-8")).hexdigest()
    copy = next(
        text
        for n in range(1, 64)
        if hashlib.sha256((text := CSV_FIXTURE + "\n" * n).encode("utf-8")).hexdigest()
        > first
    )
    ledger.file("vancity/export-copy.csv", copy)

    out = derive(ledger)

    assert (out.parsed_now, out.kept) == (1, 1)
    assert statuses(out) == {"parsed": 1, "superseded": 1}
    derived = digest(ledger)
    rebuild(ledger)
    assert digest(ledger) == derived

    # The copy leaves: the kept first copy is no longer superseded, and its lines are
    # txns again.
    (ledger.archive / "vancity/export-copy.csv").unlink()
    out = derive(ledger)

    assert out.kept == 1
    assert statuses(out) == {"parsed": 1, "unparsed": 1}
    assert out.txns == 6
    derived = digest(ledger)
    rebuild(ledger)
    assert digest(ledger) == derived


def test_a_file_that_leaves_and_comes_back_is_parsed_again(ledger: Ledger) -> None:
    seed(ledger)
    derive(ledger)
    before = digest(ledger)
    path = ledger.archive / "vancity/export.csv"
    held = path.read_bytes()

    path.unlink()
    gone = derive(ledger)
    assert statuses(gone)["unparsed"] == 2
    assert digest(ledger) != before

    path.write_bytes(held)
    back = derive(ledger)
    assert statuses(back) == SEEDED
    assert digest(ledger) == before


def test_the_digest_changes_when_a_document_changes(ledger: Ledger) -> None:
    seed(ledger)
    rebuild(ledger)
    before = digest(ledger)

    path = ledger.archive / "synthetic-card/2024-03-21-export.csv"
    text = path.read_text(encoding="utf-8")
    changed = text.replace("SAMPLE", "SAMPLX", 1)
    assert changed != text
    path.write_text(changed, encoding="utf-8")
    rebuild(ledger)

    assert digest(ledger) != before


def test_the_digest_reads_rows_and_leaves_out_written_at_times(ledger: Ledger) -> None:
    seed(ledger)
    rebuild(ledger)
    before = digest(ledger)

    later = dt.datetime(2030, 1, 1, tzinfo=dt.UTC)
    ledger.conn.execute(update(document).values(imported_at=later))
    ledger.conn.execute(update(sink_spec).values(computed_at=later))
    assert digest(ledger) == before

    one = ledger.conn.scalar(select(txn.c.id).order_by(txn.c.id).limit(1))
    ledger.conn.execute(
        update(txn).where(txn.c.id == one).values(description_norm="edited")
    )
    assert digest(ledger) != before


def test_every_derived_table_is_digested_and_no_state_table_is() -> None:
    tables = {
        table.name
        for table in schema.metadata.tables.values()
        if not table.name.startswith("report_")
    }
    assert {table.name for table in digested_tables()} == tables - derive_run.STATE


def test_every_write_time_column_of_a_digested_table_is_left_out() -> None:
    # ``occurred_at`` is when a movement happened: data, digested like any date.
    times = {
        (table.name, column.name)
        for table in digested_tables()
        for column in table.columns
        if isinstance(column.type, DateTime) and column.name != "occurred_at"
    }
    assert times == {
        (name, column)
        for name, columns in LEFT_OUT.items()
        for column in columns
        if column != "parse_inputs"
    }


# --- decisions survive ----------------------------------------------------------------


def line_ref(conn: Connection, n: int) -> str:
    keys = conn.scalars(
        select(txn_source.c.line_key_id)
        .where(txn_source.c.role == "primary")
        .order_by(txn_source.c.line_key_id)
    ).all()
    return format_ref("finance", "txn", keys[n])


def test_decisions_recorded_before_a_rebuild_are_present_after_it(
    ledger: Ledger,
) -> None:
    seed(ledger)
    derive(ledger)
    ledger.ctx = dataclasses.replace(ledger.ctx, snapshot_provider=txn_snapshot)
    refs = [line_ref(ledger.conn, n) for n in range(3)]
    obligation = format_ref(
        "finance", "obligation", uuid7(dt.datetime(2026, 1, 1, tzinfo=dt.UTC))
    )
    for payload in (
        {"kind": "category", "target_ref": refs[0], "category": "groceries"},
        {"kind": "pair", "target_ref": refs[1], "counterpart_ref": refs[2]},
        {"kind": "payment_apply", "target_ref": refs[2], "obligation_ref": obligation},
    ):
        op(ledger, "finance.decision.record", payload)
    decisions = ledger.conn.execute(select(decision).order_by(decision.c.id)).all()
    targets = ledger.conn.execute(
        select(decision_target).order_by(*decision_target.primary_key.columns)
    ).all()
    snapshots = [txn_snapshot(ledger.uow, ref) for ref in refs]

    rebuild(ledger)

    assert len(decisions) == 3
    assert ledger.conn.execute(select(decision).order_by(decision.c.id)).all() == (
        decisions
    )
    assert (
        ledger.conn.execute(
            select(decision_target).order_by(*decision_target.primary_key.columns)
        ).all()
        == targets
    )
    # The lines they name are still there, unchanged.
    assert [txn_snapshot(ledger.uow, ref) for ref in refs] == snapshots
    assert None not in snapshots


# --- no model, no network -------------------------------------------------------------


class RaisingModelClient:
    """A model client any use of which fails the test."""

    def __getattr__(self, name: str) -> NoReturn:
        raise AssertionError(f"a model client was used: {name}")


@pytest.fixture
def offline(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Every new socket connection fails. The test's database connection is already
    open, so the rolled-back transaction still works."""

    def refuse(_self: socket.socket, *_args: object) -> NoReturn:
        raise AssertionError("a network connection was attempted")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket.socket, "connect_ex", refuse)
    yield


def test_rebuild_completes_with_the_model_client_and_network_disabled(
    ledger: Ledger, offline: None
) -> None:
    # The control: a connection really is refused.
    with pytest.raises(AssertionError, match="network connection"):
        socket.create_connection(("127.0.0.1", 9), timeout=1)
    seed(ledger)
    ledger.ctx = dataclasses.replace(ledger.ctx, model_client=RaisingModelClient())

    out = rebuild(ledger)
    again = derive(ledger)

    assert statuses(out) == statuses(again) == SEEDED
    assert ledger.conn.scalar(select(func.count()).select_from(txn)) == out.txns


def test_nothing_the_ledger_operations_import_reaches_the_http_module() -> None:
    graph = {
        module.name: {each.name for each in imports(module)}
        for module in modules(PACKAGE)
    }
    seen: set[str] = set()
    todo = ["rheo_finance.derive.ops"]
    while todo:
        name = todo.pop()
        if name in seen or name not in graph:
            continue
        seen.add(name)
        todo.extend(graph[name])
    assert "rheo_finance.derive.run" in seen, "the control: the walk follows imports"
    assert not [name for name in seen if names_module(name, "rheo_finance.http")]


# --- the input fingerprint ------------------------------------------------------------


def with_zone(ledger: Ledger, clock: FakeClock, extra: str = "") -> None:
    ledger.ctx = dataclasses.replace(
        ledger.ctx,
        clock=clock,
        settings=parse_settings(
            f'[finance.paths]\narchive = "{ledger.archive}"\n'
            f"[finance.locale]\n{VANCOUVER}\n{extra}"
        ),
    )


def taken(ledger: Ledger) -> str:
    return fingerprint(ledger.uow, ledger.ctx).digest


def test_the_fingerprint_moves_at_local_midnight_and_not_at_utc_midnight(
    ledger: Ledger,
) -> None:
    seed(ledger)
    # 23:30 on 2026-01-14 in Vancouver (07:30 UTC on the 15th).
    clock = FakeClock(dt.datetime(2026, 1, 15, 7, 30, tzinfo=dt.UTC))
    with_zone(ledger, clock)
    before_local_midnight = fingerprint(ledger.uow, ledger.ctx)

    clock.advance(dt.timedelta(hours=1))  # 00:30 local on the 15th
    after_local_midnight = fingerprint(ledger.uow, ledger.ctx)

    assert before_local_midnight.local_date == dt.date(2026, 1, 14)
    assert after_local_midnight.local_date == dt.date(2026, 1, 15)
    assert after_local_midnight.digest != before_local_midnight.digest

    # 15:30 local, 23:30 UTC; then UTC midnight passes and the local day does not.
    clock.set(dt.datetime(2026, 1, 15, 23, 30, tzinfo=dt.UTC))
    afternoon = fingerprint(ledger.uow, ledger.ctx)
    clock.advance(dt.timedelta(hours=1))
    past_utc_midnight = fingerprint(ledger.uow, ledger.ctx)

    assert afternoon.digest == after_local_midnight.digest
    assert past_utc_midnight.local_date == dt.date(2026, 1, 15)
    assert past_utc_midnight.digest == afternoon.digest


def test_the_fingerprint_moves_with_each_input_and_only_then(
    ledger: Ledger, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed(ledger)
    with_zone(ledger, FakeClock(dt.datetime(2026, 1, 15, 20, 0, tzinfo=dt.UTC)))
    start = taken(ledger)
    assert taken(ledger) == start

    # A derive writes no input.
    derive(ledger)
    assert taken(ledger) == start

    seen = {start}

    def moved() -> None:
        now = taken(ledger)
        assert now not in seen
        seen.add(now)

    ledger.file("synthetic-card/2025-03-01-more.csv", "NOT A CARD DOCUMENT EITHER\n")
    moved()
    ledger.add_identifier("card", "999000000066")
    moved()
    ledger.ctx = dataclasses.replace(ledger.ctx, snapshot_provider=txn_snapshot)
    op(
        ledger,
        "finance.decision.record",
        {"kind": "category", "target_ref": line_ref(ledger.conn, 0), "tag": "x"},
    )
    moved()
    with_zone(
        ledger,
        FakeClock(dt.datetime(2026, 1, 15, 20, 0, tzinfo=dt.UTC)),
        "[finance.import]\noverlap_day_tolerance = 9\n",
    )
    moved()
    csv_parser = next(p for p in CARD_PARSERS if p.parser_id.endswith("_csv"))
    monkeypatch.setitem(
        registry.PARSERS,
        csv_parser.parser_id,
        dataclasses.replace(csv_parser, version="3"),
    )
    moved()


def test_derive_and_rebuild_return_the_fingerprint_they_used(ledger: Ledger) -> None:
    seed(ledger)
    expected = taken(ledger)

    assert derive(ledger).fingerprint == expected
    assert rebuild(ledger).fingerprint == expected


# --- the catalogue --------------------------------------------------------------------


def test_the_ledger_operations_are_catalogued_as_the_spec_names_them() -> None:
    specs = catalogue.load_all()
    owner_and_service = frozenset({Role.OWNER, Role.SERVICE})
    for name in ("finance.ledger.derive", "finance.ledger.rebuild"):
        spec = specs[name]
        assert spec.safety_class is SafetyClass.MUTATE
        assert (spec.connection, spec.roles) == ("owner", owner_and_service)
    spec = specs["finance.ledger.digest"]
    assert spec.safety_class is SafetyClass.READ
    assert (spec.connection, spec.roles, spec.tool_name) == (
        "owner",
        owner_and_service,
        None,
    )
