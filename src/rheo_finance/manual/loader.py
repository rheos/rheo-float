"""The generic manual-entry CSV loader (FR-10, AC-10).

``load_csv(path, row_model)`` reads one manual file and validates each row with its
own pydantic model. An invalid row becomes a ``RowError`` with its file and line number
and every other row loads. A missing file loads as empty. A header that is not exactly
the file's columns in ``manual.headers.MANUAL_FILES`` (same names, same order) is one
file-level error on line 1 and no row loads.

Cells are stripped; a blank cell is left out, so the model's default applies (or the
field is reported missing). A list cell holds its values separated by ``;``
(``split_list``). A blank line is skipped. A row that is not valid CSV (a stray
quote) is an error and the next row still loads; an unclosed quote runs to the end of
the file, so that row is the last. A row's line number is the physical line it
starts on, so a quoted cell spanning lines still points at its row.

A reason names the column and what is wrong with it, never the cell's value: a cell
can hold an identifier, and reasons are stored and printed.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

from pydantic import BaseModel, ValidationError

from rheo_finance.manual.headers import MANUAL_FILES

LIST_SEPARATOR: Final = ";"
_ENCODING: Final = "utf-8-sig"  # a spreadsheet's byte-order mark is not a column


@dataclass(frozen=True, order=True)
class RowError:
    """An invalid row (or, on line 1, an invalid file): ``file`` is the file name."""

    file: str
    line_no: int
    reason: str


@dataclass
class LoadResult[T: BaseModel]:
    """The rows that validated, in file order, and the errors.

    ``line_numbers[i]`` is the line ``rows[i]`` starts on, for rules that span rows.
    """

    rows: list[T] = field(default_factory=list)
    errors: list[RowError] = field(default_factory=list)
    line_numbers: list[int] = field(default_factory=list)

    def numbered(self) -> list[tuple[int, T]]:
        return list(zip(self.line_numbers, self.rows, strict=True))


def split_list(value: object) -> object:
    """A ``;``-separated cell to a tuple of its non-blank parts (a pydantic
    ``BeforeValidator``); anything else passes through for the model to judge."""
    if isinstance(value, str):
        return tuple(
            part.strip() for part in value.split(LIST_SEPARATOR) if part.strip()
        )
    return value


def _reason(exc: ValidationError) -> str:
    parts = []
    for error in exc.errors(include_input=False, include_url=False):
        where = ".".join(str(part) for part in error["loc"])
        parts.append(f"{where}: {error['msg']}" if where else error["msg"])
    return "; ".join(parts)


def load_csv[T: BaseModel](path: Path, row_model: type[T]) -> LoadResult[T]:
    """Load ``path`` (a file named in ``MANUAL_FILES``), one ``row_model`` per row."""
    name = path.name
    if name not in MANUAL_FILES:
        raise ValueError(f"{name} is not a manual-entry file")
    columns = MANUAL_FILES[name]
    if set(columns) != set(row_model.model_fields):
        raise TypeError(f"{row_model.__name__} does not have the columns of {name}")
    result: LoadResult[T] = LoadResult()
    try:
        text = path.read_text(encoding=_ENCODING)
    except FileNotFoundError:
        return result
    except UnicodeDecodeError:
        result.errors.append(RowError(name, 1, "the file is not UTF-8 text"))
        return result

    reader = csv.reader(io.StringIO(text, newline=""), strict=True)
    try:
        header = next(reader, None)
    except csv.Error:
        header = None
    if header is None or tuple(cell.strip() for cell in header) != columns:
        result.errors.append(
            RowError(name, 1, f"the header is not: {','.join(columns)}")
        )
        return result

    while True:
        start = reader.line_num + 1
        try:
            cells = next(reader, None)
        except csv.Error:
            result.errors.append(RowError(name, start, "the row is not valid CSV"))
            if reader.line_num < start:
                break  # no line consumed: nothing left to resume from
            # The reader resumes on the next line; an unclosed quote runs to the end.
            continue
        if cells is None:
            break
        values = [cell.strip() for cell in cells]
        if not any(values):
            continue
        if len(values) != len(columns):
            result.errors.append(
                RowError(
                    name,
                    start,
                    f"the row has {len(values)} cells, the header {len(columns)}",
                )
            )
            continue
        data = {
            column: value
            for column, value in zip(columns, values, strict=True)
            if value
        }
        try:
            row = row_model.model_validate(data)
        except ValidationError as exc:
            result.errors.append(RowError(name, start, _reason(exc)))
            continue
        result.rows.append(row)
        result.line_numbers.append(start)
    return result
