"""``manual.loader.load_csv``: per-row validation, line numbers, file-level errors.

Synthetic CSV text in a temporary directory; no database.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import BaseModel

from rheo_finance.manual.accounts import AccountIdentifierRow, AccountRow
from rheo_finance.manual.headers import MANUAL_FILES
from rheo_finance.manual.loader import RowError, load_csv

NAME = "account_identifiers.csv"
HEADER = "account,identifier,kind,valid_from,valid_to\n"


def write(tmp_path: Path, body: str, name: str = NAME) -> Path:
    path = tmp_path / name
    path.write_text(body, encoding="utf-8")
    return path


@pytest.mark.parametrize(
    ("name", "model"),
    [("accounts.csv", AccountRow), (NAME, AccountIdentifierRow)],
)
def test_each_row_model_has_its_files_columns(
    name: str, model: type[BaseModel]
) -> None:
    assert tuple(model.model_fields) == MANUAL_FILES[name]


def test_a_missing_file_loads_as_empty(tmp_path: Path) -> None:
    result = load_csv(tmp_path / NAME, AccountIdentifierRow)
    assert (result.rows, result.errors) == ([], [])


def test_an_invalid_row_is_reported_by_line_and_the_rest_load(tmp_path: Path) -> None:
    path = write(
        tmp_path,
        HEADER
        + "test-a,9991234,number,,\n"
        + "\n"  # a blank line is skipped, and still counted
        + "test-b,9995678,number,not-a-date,\n"
        + "test-c,9998765,file_token,,\n",
    )
    result = load_csv(path, AccountIdentifierRow)

    assert [row.account for row in result.rows] == ["test-a", "test-c"]
    assert result.line_numbers == [2, 5]
    assert [(e.file, e.line_no) for e in result.errors] == [(NAME, 4)]
    assert result.errors[0].reason.startswith("valid_from: ")


def test_a_row_spanning_lines_reports_the_line_it_starts_on(tmp_path: Path) -> None:
    path = write(
        tmp_path,
        HEADER + '"test-a",9991234,number,,\n"test-b","999\n5678",bogus,,\n',
    )
    result = load_csv(path, AccountIdentifierRow)

    assert [(e.line_no, e.reason.split(":")[0]) for e in result.errors] == [(3, "kind")]


def test_a_row_with_the_wrong_cell_count_is_an_error(tmp_path: Path) -> None:
    path = write(tmp_path, HEADER + "test-a,9991234,number\ntest-b,9995678,number,,\n")
    result = load_csv(path, AccountIdentifierRow)

    assert [row.account for row in result.rows] == ["test-b"]
    assert result.errors == [RowError(NAME, 2, "the row has 3 cells, the header 5")]


@pytest.mark.parametrize(
    "body",
    [
        "",
        "account,identifier,kind,valid_from\ntest-a,9991234,number,\n",
        "identifier,account,kind,valid_from,valid_to\n9991234,test-a,number,,\n",
    ],
)
def test_a_header_that_does_not_match_is_one_file_level_error(
    tmp_path: Path, body: str
) -> None:
    result = load_csv(write(tmp_path, body), AccountIdentifierRow)

    assert result.rows == []
    assert [(e.file, e.line_no) for e in result.errors] == [(NAME, 1)]


def test_a_byte_order_mark_and_padded_cells_are_accepted(tmp_path: Path) -> None:
    path = tmp_path / NAME
    path.write_bytes(("﻿" + HEADER + " test-a , 9991234 ,number,,\n").encode())
    result = load_csv(path, AccountIdentifierRow)

    assert result.errors == []
    assert [(row.account, row.identifier) for row in result.rows] == [
        ("test-a", "9991234")
    ]


def test_list_cells_split_on_semicolons(tmp_path: Path) -> None:
    header = ",".join(MANUAL_FILES["accounts.csv"]) + "\n"
    row = "test-a,Test,Example Bank,wallet,cad; BTC ;,none,,,,mail,a.test;b.test,,,,\n"
    result = load_csv(write(tmp_path, header + row, "accounts.csv"), AccountRow)

    assert result.errors == []
    (account,) = result.rows
    assert account.currencies == ("CAD", "BTC")
    assert account.mail_senders == ("a.test", "b.test")
    assert account.payee_patterns == ()
    assert account.placeholder is False


def test_a_file_the_headers_do_not_name_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="not a manual-entry file"):
        load_csv(tmp_path / "other.csv", AccountIdentifierRow)
