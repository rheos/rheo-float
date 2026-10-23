"""Stage 1's ``superseded``: of two documents that rank equally on an account for
exactly the same period, the larger sha256 stays primary (SPEC "Derivation" stage 1).

Seam: ``derive.run`` over two copies of one document whose bytes differ, read back
through ``document`` and the txn tables. Every value is synthetic.
"""

from __future__ import annotations

import hashlib

from sqlalchemy import func, select

from rheo_finance.storage.schema import document, txn, txn_source

from .support import FIXTURES, Ledger, card_fixture

CSV_FIXTURE = (FIXTURES / "vancity_chequing_jump.csv").read_text(encoding="utf-8")
STATEMENT = (FIXTURES / "vancity_statement_split.txt").read_text(encoding="utf-8")


def statuses(ledger: Ledger) -> dict[str, str]:
    return {
        row.sha256: row.status
        for row in ledger.conn.execute(select(document.c.sha256, document.c.status))
    }


def sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def counts(ledger: Ledger) -> tuple[int, int]:
    return (
        int(ledger.conn.scalar(select(func.count()).select_from(txn)) or 0),
        int(ledger.conn.scalar(select(func.count()).select_from(txn_source)) or 0),
    )


def test_two_copies_of_an_export_keep_the_larger_sha256(ledger: Ledger) -> None:
    ledger.chequing()
    copies = [CSV_FIXTURE, CSV_FIXTURE + "\n"]
    for number, text in enumerate(copies):
        ledger.file(f"vancity/export-{number}.csv", text)

    out = ledger.derive()

    assert out.documents == {"parsed": 1, "superseded": 1}
    larger, smaller = sorted((sha(each) for each in copies), reverse=True)
    assert statuses(ledger) == {larger: "parsed", smaller: "superseded"}
    # Only the primary copy's lines are read: no additional source either.
    assert counts(ledger) == (6, 6)


def test_two_copies_of_a_statement_keep_the_larger_sha256(ledger: Ledger) -> None:
    ledger.chequing()
    copies = [STATEMENT, STATEMENT + "\n\n"]
    for number, text in enumerate(copies):
        ledger.file(f"vancity/statement-{number}.pdf", text)

    out = ledger.derive()

    assert out.documents == {"parsed": 1, "superseded": 1}
    larger, smaller = sorted((sha(each) for each in copies), reverse=True)
    assert statuses(ledger) == {larger: "parsed", smaller: "superseded"}
    assert counts(ledger) == (3, 3)


def test_documents_of_different_rank_or_period_are_not_superseded(
    ledger: Ledger,
) -> None:
    ledger.chequing()
    rows = CSV_FIXTURE.splitlines()
    ledger.file("vancity/whole.csv", CSV_FIXTURE)
    # The same export less its oldest row: a shorter period.
    ledger.file("vancity/shorter.csv", "\n".join(rows[:-1]) + "\n")
    ledger.file("vancity/2026-01-10.pdf", STATEMENT)

    out = ledger.derive()

    assert out.documents == {"parsed": 3}
    assert set(statuses(ledger).values()) == {"parsed"}


def test_a_superseded_copy_is_parsed_again_when_its_twin_leaves(
    ledger: Ledger,
) -> None:
    ledger.chequing()
    copies = [CSV_FIXTURE, CSV_FIXTURE + "\n"]
    paths = [f"vancity/export-{number}.csv" for number in range(2)]
    for path, text in zip(paths, copies, strict=True):
        ledger.file(path, text)
    ledger.derive()
    larger = max(copies, key=sha)

    # The primary copy's file leaves the archive: it is unparsed on the next derive,
    # and the other copy is read instead.
    (ledger.archive / paths[copies.index(larger)]).unlink()
    out = ledger.derive()

    assert out.documents == {"parsed": 1, "unparsed": 1}
    assert counts(ledger) == (6, 6)


def test_an_export_and_a_download_of_one_period_are_not_superseded(
    ledger: Ledger, card_parsers: None
) -> None:
    # Equal control (both unverified) and the same period, but different formats: a
    # transaction download outranks a CSV, so neither copy is superseded.
    ledger.card()
    text = card_fixture("card_2024_export.csv")
    ledger.file("synthetic-card/2024-03-21-export.csv", text)
    ledger.file("synthetic-card/2024-03-21-download.pdf", text + "\n")

    out = ledger.derive()

    assert out.documents == {"parsed": 2}
