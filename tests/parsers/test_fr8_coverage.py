"""Every source kind FR-8 names has a registered parser, a listed synthetic fixture
and a passing parse (AC-8, parser half).

``FR8_KINDS`` is FR-8's own list, written out here, and ``SOURCES`` maps each kind to
the (issuer, format, parser id, version) that reads it, the fixture it is tested on
and the accounts that fixture names. The tests fail when a kind has no source, when
its parser is not registered (a module missing from ``PARSER_MODULES`` included),
when its fixture is not in ``MANIFEST.toml``, or when ``parse_document`` does not
parse the fixture with that parser, tied out where its control kind checks one. An
issuer directory that gains a parser version without a fixture fails too.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import hashlib
import tomllib
from pathlib import Path, PurePosixPath

import pytest
from sqlalchemy import Connection, select

from rheo_finance.context import UnitOfWork
from rheo_finance.documents.issuers import place
from rheo_finance.documents.registry import document_id, file_archive
from rheo_finance.parsers.registry import parse_document, parsers_for
from rheo_finance.storage.schema import document, statement_period

from .conftest import ManualAccount, register_accounts

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "tests" / "fixtures" / "synthetic"
MANIFEST = FIXTURES / "MANIFEST.toml"
NOW = dt.datetime(2026, 4, 1, 12, 0, tzinfo=dt.UTC)

# FR-8's list of on-disk source kinds, in its order.
FR8_KINDS = (
    "Vancity Visa PDF statement",
    "Vancity Visa CSV export",
    "Vancity Visa PDF transaction export",
    "MBNA statement",
    "Wise transaction history PDF",
    "Nelson & District Credit Union statement (current layout)",
    "Nelson & District Credit Union statement (2018 layout)",
    "PayPal activity statement PDF",
    "Vancity chequing CSV export",
    "FortisBC bill PDF",
    "Invoices received",
    "Receipts",
)


A = ManualAccount
VISA = A("visa", "Vancity", "credit_card", ("9999 99** **** 9991",), "masked_card")
MBNA = A("mbna-card", "MBNA", "credit_card", ("9999 99XX XXXX 9992",), "masked_card")
WISE = A("wise", "Wise", "wallet", ("P99900001",), currencies="CAD;USD")
CU = "Nelson & District Credit Union"
CU_CHEQUING = A("cu-chequing", CU, "chequing", ("999000000101",))
CU_SHARES = A("cu-shares", CU, "savings", ("999000000102",))
PAYPAL = A(
    "paypal", "PayPal", "payment_processor", ("SAMPLE999A9ID",), currencies="CAD;USD"
)
CHEQUING = A("chequing", "Vancity", "chequing", ("999000000077-999-99901-99902",))
HYDRO = A("hydro", "FortisBC", "bill_vendor", ("9990001234",))


@dataclasses.dataclass(frozen=True)
class Source:
    path: str  # where the fixture is filed in the archive
    fixture: str
    parser_id: str
    version: str
    accounts: tuple[ManualAccount, ...] = ()


SOURCES: dict[str, Source] = {
    "Vancity Visa PDF statement": Source(
        "vancity-visa/2026-01-05.pdf", "vancity_visa_statement.txt",
        "vancity_visa.statement", "1", (VISA,),
    ),
    "Vancity Visa CSV export": Source(
        "vancity-visa/2024-02-21.csv", "vancity_visa_export.csv",
        "vancity_visa.csv", "1", (VISA,),
    ),
    "Vancity Visa PDF transaction export": Source(
        "vancity-visa/2025-10-20-download.pdf", "vancity_visa_transactions.txt",
        "vancity_visa.transactions", "transactions-1", (VISA,),
    ),
    "MBNA statement": Source(
        "mbna/2025-09-20.pdf", "mbna_statement.txt", "mbna.statement", "1", (MBNA,),
    ),
    "Wise transaction history PDF": Source(
        "wise/transactions-2026-02-01_to_2026-02-28.pdf", "wise_history.txt",
        "wise.history", "1", (WISE,),
    ),
    "Nelson & District Credit Union statement (current layout)": Source(
        "nelson-district-cu/2025-11-30.pdf", "ndcu_statement_current.txt",
        "nelson_district_cu.statement", "1", (CU_CHEQUING, CU_SHARES),
    ),
    "Nelson & District Credit Union statement (2018 layout)": Source(
        "nelson-district-cu/2018-08-31.pdf", "ndcu_statement_2018.txt",
        "nelson_district_cu.statement_2018", "2", (CU_CHEQUING, CU_SHARES),
    ),
    "PayPal activity statement PDF": Source(
        "paypal/activity-2026-04-01_to_2026-04-30.pdf", "paypal_activity.txt",
        "paypal.activity", "1", (PAYPAL,),
    ),
    "Vancity chequing CSV export": Source(
        "vancity/transactions-2026-03.csv", "vancity_chequing_hints.csv",
        "vancity.chequing_csv", "2", (CHEQUING,),
    ),
    "FortisBC bill PDF": Source(
        "fortisbc/2026-01-15.pdf", "fortisbc_bill.txt", "fortisbc.bill", "1", (HYDRO,),
    ),
    "Invoices received": Source(
        "invoices-received/sample-vendor/2025-12-01.pdf", "invoice_received.txt",
        "invoices_received.invoice", "1",
    ),
    "Receipts": Source(
        "receipts/sample-clinic/2026-03-16.pdf", "patient_receipt.txt",
        "receipts.patient_receipt", "1",
    ),
}  # fmt: skip


def issuer_and_format(source: Source) -> tuple[str, str]:
    placed = place(PurePosixPath(source.path))
    assert placed is not None, source.path
    return placed.issuer, placed.format


def test_every_fr8_kind_has_a_source() -> None:
    assert tuple(SOURCES) == FR8_KINDS


@pytest.mark.parametrize("kind", FR8_KINDS)
def test_the_kind_has_a_registered_parser(kind: str) -> None:
    source = SOURCES[kind]
    registered = {
        (each.parser_id, each.version)
        for each in parsers_for(*issuer_and_format(source))
    }
    assert (source.parser_id, source.version) in registered


@pytest.mark.parametrize("kind", FR8_KINDS)
def test_the_kind_has_a_listed_synthetic_fixture(kind: str) -> None:
    listed = {
        entry["path"]: entry
        for entry in tomllib.loads(MANIFEST.read_text(encoding="utf-8"))["fixture"]
    }
    path = f"tests/fixtures/synthetic/{SOURCES[kind].fixture}"
    assert (ROOT / path).is_file()
    assert listed[path]["synthetic"] is True


def test_every_parser_of_an_fr8_directory_has_a_source() -> None:
    """A parser version added for one of these directories needs its own source
    here, and so a fixture and a parse."""
    wanted = {issuer_and_format(source) for source in SOURCES.values()}
    registered = {
        each.parser_id for issuer, fmt in wanted for each in parsers_for(issuer, fmt)
    }
    assert registered == {source.parser_id for source in SOURCES.values()}


@pytest.mark.parametrize("kind", FR8_KINDS)
def test_the_kind_parses_its_fixture(
    kind: str, owner_conn: Connection, tmp_path: Path
) -> None:
    """``parse_document`` parses the fixture with the kind's parser, and every period
    whose control kind checks one (``summary``, ``running_balance``,
    ``bill_identity``) is tied out."""
    source = SOURCES[kind]
    data, archive = tmp_path / "data", tmp_path / "archive"
    archive.mkdir()
    uow = UnitOfWork(owner_conn)
    register_accounts(uow, data, *source.accounts)
    raw = (FIXTURES / source.fixture).read_bytes()
    target = archive / source.path
    target.parent.mkdir(parents=True)
    target.write_bytes(raw)
    file_archive(uow, archive, data, NOW)
    doc = document_id(hashlib.sha256(raw).hexdigest())

    parsed = parse_document(
        uow, doc, archive, extract_pdf=lambda path: path.read_text(encoding="utf-8")
    )

    assert (parsed.status, parsed.reason) == ("parsed", None)
    assert parsed.lines + parsed.periods > 0
    row = owner_conn.execute(select(document).where(document.c.id == doc)).one()
    assert (row.parser_id, row.parser_version) == (source.parser_id, source.version)
    periods = owner_conn.execute(
        select(statement_period.c.control_kind, statement_period.c.tied_out).where(
            statement_period.c.document_id == doc
        )
    ).all()
    assert all(tied for control, tied in periods if control != "none")
