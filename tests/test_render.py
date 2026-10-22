"""``render``: the headline budget, the value formats and the one output choke point."""

from __future__ import annotations

import argparse
import ast
import datetime as dt
import io
import json
import subprocess
import sys
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from rheo_finance import db, render
from rheo_finance.cli import main as cli_main
from rheo_finance.clock import FakeClock
from rheo_finance.render import (
    Report,
    as_of,
    cap_rows,
    emit,
    emit_json,
    guard,
    iso_date,
    json_text,
    money,
)

EM = chr(0x2014)  # built at runtime so this file holds no U+2014 itself
SRC = Path(__file__).resolve().parents[1] / "src" / "rheo_finance"


def emitted(value: Report | str) -> str:
    stream = io.StringIO()
    emit(value, stream)
    return stream.getvalue()


# --- guard -----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "guarded"),
    [
        (f"a{EM}b", "a - b"),  # no spaces around the dash: a spaced hyphen
        (f"a {EM} b", "a - b"),  # spaces collapse to one on each side
        (f"a  \t{EM}{EM}  b", "a - b"),  # a run of dashes is one hyphen
        (f"{EM}x", "- x"),  # line start: no leading space
        (f"x{EM}", "x -"),  # line end: no trailing space
        (f"one\n{EM} two", "one\n- two"),  # line breaks are kept
        ("plain - text", "plain - text"),  # nothing to replace
    ],
)
def test_guard_replaces_every_em_dash_with_a_plain_hyphen(
    raw: str, guarded: str
) -> None:
    assert guard(raw) == guarded
    assert EM not in guard(raw)


NBSP, THIN, NARROW_NBSP = chr(0x00A0), chr(0x2009), chr(0x202F)


@pytest.mark.parametrize(
    "space",
    [" ", "\t", NBSP, THIN, NARROW_NBSP, f" {NBSP}\t{THIN}{NARROW_NBSP}"],
    ids=["space", "tab", "no-break", "thin", "narrow-no-break", "mixed"],
)
def test_guard_collapses_all_horizontal_whitespace_around_the_dash(space: str) -> None:
    assert guard(f"a{space}{EM}{space}b") == "a - b"
    assert guard(f"a{space}{EM}{space}{EM}{space}b") == "a - b"
    # a line break is not horizontal space: it stays, and the hyphen hugs the text
    assert guard(f"a{space}\n{space}{EM}{space}b") == f"a{space}\n- b"


# --- emit: the choke point -------------------------------------------------------


def test_text_holding_an_em_dash_never_reaches_the_stream() -> None:
    out = emitted(f"finance owed: 3 bills {EM} 1 overdue")
    assert out == "finance owed: 3 bills - 1 overdue\n"


def test_a_report_holding_an_em_dash_never_reaches_the_stream() -> None:
    report = Report([f"headline {EM} one"], [f"{EM} detail", f"x{EM}y"])
    out = emitted(report)
    assert EM not in out
    assert out == "headline - one\n\n- detail\nx - y\n"


def test_emit_writes_to_stdout_by_default(capsys: pytest.CaptureFixture[str]) -> None:
    emit(f"a{EM}b")
    assert capsys.readouterr().out == "a - b\n"


def test_emit_takes_only_a_report_or_text() -> None:
    with pytest.raises(TypeError):
        emit(["a list"], io.StringIO())  # type: ignore[arg-type]


def test_json_output_guards_every_string_value_and_key() -> None:
    payload = {
        f"key{EM}one": f"value {EM} one",
        "nested": [{"label": f"{EM}x"}, ("t", f"u{EM}")],
        "amount": Decimal("1E-7"),
        "day": dt.date(2026, 1, 5),
        "at": dt.datetime(2026, 1, 5, 7, 0, tzinfo=dt.UTC),
        "count": 3,
        "flag": True,
        "none": None,
    }
    text = json_text(payload)
    assert EM not in text
    assert EM not in json.dumps(json.loads(text), ensure_ascii=False)
    loaded = json.loads(text)
    assert loaded["key - one"] == "value - one"
    assert loaded["nested"] == [{"label": "- x"}, ["t", "u -"]]
    assert loaded["amount"] == "0.0000001"  # plain-form string, never a float
    assert loaded["day"] == "2026-01-05"
    assert loaded["at"] == "2026-01-05T07:00:00+00:00"

    stream = io.StringIO()
    emit_json(payload, stream)
    assert stream.getvalue() == text


def test_json_output_guards_a_pydantic_model() -> None:
    from pydantic import BaseModel

    class Row(BaseModel):
        label: str
        amount: Decimal

    loaded = json.loads(json_text(Row(label=f"a{EM}b", amount=Decimal("1.50"))))
    assert loaded == {"label": "a - b", "amount": "1.50"}


def test_json_output_refuses_keys_that_collide_once_guarded() -> None:
    with pytest.raises(ValueError, match="once guarded"):
        json_text({f"a{EM}b": 1, "a - b": 2})


def test_json_output_refuses_a_float() -> None:
    with pytest.raises(TypeError, match="float"):
        json_text({"amount": 1.5})


# --- Report: the headline budget -------------------------------------------------


def test_a_six_line_headline_is_accepted_and_seven_refused() -> None:
    six = [f"line {n}" for n in range(1, 7)]
    assert Report(six, ["detail"]).headline == tuple(six)
    with pytest.raises(ValueError, match="at most 6 lines"):
        Report([*six, "line 7"])


def test_a_report_needs_a_headline_of_single_lines() -> None:
    with pytest.raises(ValueError, match="at least one line"):
        Report([], ["detail"])
    with pytest.raises(ValueError, match="line break"):
        Report(["one\ntwo"])  # a hidden second line would dodge the budget
    with pytest.raises(ValueError, match="line break"):
        Report(["ok"], ["three\nfour"])
    with pytest.raises(ValueError, match="line break"):
        Report([f"five{chr(0x2028)}six"])  # splitlines() breaks here too
    with pytest.raises(TypeError):
        Report("a string, not a list")


def test_a_report_cannot_be_changed_after_its_budget_is_checked() -> None:
    report = Report(["one"])
    with pytest.raises(AttributeError):
        report.headline = tuple(f"line {n}" for n in range(9))
    assert isinstance(report.headline, tuple)


def test_a_status_list_of_15_lines_is_accepted_and_16_refused() -> None:
    lines = [f"status line {n}" for n in range(1, 16)]
    report = Report.from_status_lines(lines)
    assert report.headline == tuple(lines[:6])
    assert report.detail == tuple(lines[6:])
    # status is one fixed list: it prints in order with no separator line
    assert emitted(report).splitlines() == lines
    with pytest.raises(ValueError, match="at most 15 lines"):
        Report.from_status_lines([*lines, "status line 16"])


# --- formats ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("amount", "currency", "text"),
    [
        (Decimal("1234.56"), "CAD", "1,234.56 CAD"),
        (Decimal("1234.5"), "CAD", "1,234.50 CAD"),
        (Decimal("0.125"), "CAD", "0.12 CAD"),  # half-even: 2 is even
        (Decimal("0.135"), "CAD", "0.14 CAD"),  # half-even: 4 is even
        (Decimal("-1234.565"), "CAD", "-1,234.56 CAD"),
        (Decimal("-0.001"), "CAD", "0.00 CAD"),  # never "-0.00"
        (Decimal("1234567.891"), "USD", "1,234,567.891 USD"),  # other currencies exact
        (Decimal("0.00000001"), "BTC", "0.00000001 BTC"),
        (Decimal("1E-7"), "BTC", "0.0000001 BTC"),  # no exponent form
    ],
)
def test_money_formats(amount: Decimal, currency: str, text: str) -> None:
    assert money(amount, currency) == text


def test_money_refuses_a_float_or_a_bad_currency() -> None:
    with pytest.raises(TypeError):
        money(1.5, "CAD")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        money(Decimal("NaN"), "CAD")
    with pytest.raises(ValueError):
        money(Decimal("1"), "cad")


def test_iso_date_is_yyyy_mm_dd_and_refuses_an_instant() -> None:
    assert iso_date(dt.date(2026, 1, 5)) == "2026-01-05"
    with pytest.raises(TypeError):
        iso_date(dt.datetime(2026, 1, 5, tzinfo=dt.UTC))


def test_as_of_renders_the_clock_in_the_zone() -> None:
    # 06:30 UTC on 2026-10-22 is 23:30 on 2026-10-21 in America/Creston (UTC-7).
    clock = FakeClock(dt.datetime(2026, 10, 22, 6, 30, tzinfo=dt.UTC))
    assert as_of(clock, ZoneInfo("America/Creston")) == "as of 2026-10-21 23:30 MST"


def test_cap_rows_folds_the_rest_into_one_more_line() -> None:
    rows = [f"row {n}" for n in range(1, 19)]
    assert cap_rows(rows[:15], 15, "more") == rows[:15]
    assert cap_rows(rows, 15, "more") == [*rows[:15], "+3 more"]
    assert cap_rows(rows, 2, "more bills")[-1] == "+16 more bills"
    with pytest.raises(ValueError):
        cap_rows(rows, 0, "more")


# --- the CLI goes through the choke point ----------------------------------------


def test_cli_help_and_usage_errors_are_guarded(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cli_main, "HEADLINE", f"finance {EM} a test headline")
    assert cli_main.main(["--help"]) == 0
    out = capsys.readouterr().out
    assert "finance - a test headline" in out
    assert EM not in out

    assert cli_main.main([f"no{EM}such"]) == 2
    err = capsys.readouterr().err
    assert "invalid choice" in err
    assert EM not in err


def test_cli_help_to_a_closed_stream_exits_quietly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class ClosedPipe(io.StringIO):
        def write(self, text: str, /) -> int:
            raise BrokenPipeError("the reader went away")

    monkeypatch.setattr(sys, "stdout", ClosedPipe())
    assert cli_main.main(["--help"]) == 0


def _with_boom(monkeypatch: pytest.MonkeyPatch, raised: BaseException) -> None:
    """Give ``main`` a ``boom`` command whose handler raises ``raised``."""
    build = cli_main.build_parser

    def handler(_args: argparse.Namespace) -> int:
        raise raised

    def build_with_boom() -> argparse.ArgumentParser:
        parser = build()
        for action in parser._actions:
            if isinstance(action, argparse._SubParsersAction):
                action.add_parser("boom").set_defaults(handler=handler)
        return parser

    monkeypatch.setattr(cli_main, "build_parser", build_with_boom)


def test_an_unexpected_failure_is_guarded_and_shows_no_sql(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    raised = RuntimeError(
        f"(psycopg.errors.Thing) bad {EM} thing [SQL: SELECT 1]\n"
        "[SQL: SELECT secret FROM t WHERE x = %(x)s]\n[parameters: {'x': 'v'}]"
    )
    _with_boom(monkeypatch, raised)
    assert cli_main.main(["boom"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == (
        "finance boom: failed. RuntimeError: (psycopg.errors.Thing) bad - thing\n"
    )
    assert EM not in captured.err


def test_an_unexpected_failure_prints_no_password(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    secret = "s3cretpw"
    raised = OSError(
        f"could not connect to postgresql://finance_owner:{secret}@127.0.0.1:55440"
        f"/finance (password={secret})"
    )
    _with_boom(monkeypatch, raised)
    assert cli_main.main(["boom"]) == 1
    captured = capsys.readouterr()
    assert secret not in captured.err + captured.out
    assert "finance_owner:<redacted>@127.0.0.1" in captured.err


def test_an_interrupt_exits_130_without_a_traceback(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _with_boom(monkeypatch, KeyboardInterrupt())
    assert cli_main.main(["boom"]) == 130
    assert capsys.readouterr().err == "finance boom: interrupted.\n"


def test_a_multi_line_command_failure_keeps_the_headline_budget(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    tail = [f"detail line {n} {EM} x" for n in range(1, 9)]

    def failing_migrate() -> str:
        raise db.DbError("could not migrate:\n" + "\n".join(tail))

    monkeypatch.setattr(db, "migrate", failing_migrate)
    assert cli_main.main(["db", "migrate"]) == 1
    lines = capsys.readouterr().err.splitlines()
    assert lines[0] == "finance db migrate: failed. could not migrate:"
    assert lines[1] == ""  # one headline line, then the detail
    assert lines[2:] == [line.replace(f" {EM} ", " - ") for line in tail]


# --- nothing writes around the choke point ---------------------------------------

STREAMS = {"stdout", "stderr", "__stdout__", "__stderr__"}
DEVICES = {"/dev/stdout", "/dev/stderr", "/dev/tty", "/dev/fd/1", "/dev/fd/2"}
OS_WRITERS = {"write", "writev"}  # os.fdopen(<fd>) is the open(<fd>) case


def bypasses(source: str, name: str = "<snippet>") -> list[str]:
    """Every way ``source`` could write to stdout or stderr without ``emit``."""
    tree = ast.parse(source)
    aliases: dict[str, set[str]] = {"sys": set(), "os": set(), "traceback": set()}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                if root in aliases:
                    aliases[root].add(alias.asname or root)
    found: list[str] = []

    def flag(node: ast.AST, what: str) -> None:
        found.append(f"{name}:{getattr(node, 'lineno', 0)} {what}")

    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id in {"print", *STREAMS}:
            flag(node, node.id)
        elif isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            base, attr = node.value.id, node.attr
            if base in aliases["sys"] and attr in STREAMS:
                flag(node, f"sys.{attr}")
            elif base in aliases["os"] and attr in OS_WRITERS:
                flag(node, f"os.{attr}")
            elif base in aliases["traceback"] and attr.startswith("print"):
                flag(node, f"traceback.{attr}")
            elif base == "builtins" and attr == "print":
                flag(node, "builtins.print")
        elif isinstance(node, ast.ImportFrom) and node.module in aliases:
            for alias in node.names:
                if (
                    (node.module == "sys" and alias.name in STREAMS)
                    or (node.module == "os" and alias.name in OS_WRITERS)
                    or (node.module == "traceback" and alias.name.startswith("print"))
                ):
                    flag(node, f"from {node.module} import {alias.name}")
        elif isinstance(node, ast.Constant) and node.value in STREAMS | DEVICES:
            flag(node, repr(node.value))
        elif (
            isinstance(node, ast.Call)
            and (
                (isinstance(node.func, ast.Name) and node.func.id == "open")
                or (
                    isinstance(node.func, ast.Attribute)
                    and node.func.attr == "fdopen"
                    and isinstance(node.func.value, ast.Name)
                    and node.func.value.id in aliases["os"]
                )
            )
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, int)
        ):
            flag(node, "open(<fd>)")
    return found


@pytest.mark.parametrize(
    "source",
    [
        "import sys\nsys.stdout.buffer.write(b'x')\n",
        "from sys import stdout\nstdout.write('x')\n",
        "import sys\nsys.__stdout__.write('x')\n",
        "import traceback\ntraceback.print_exc()\n",
        "import os\nos.write(1, b'x')\n",
        "print('x')\n",
        "say = print\n",
        "import sys as s\ns.stderr.write('x')\n",
        "from traceback import print_exception\n",
        "from os import write\n",
        "import sys\ngetattr(sys, 'stderr').write('x')\n",
        "open(1, 'w').write('x')\n",
        "import os\nos.fdopen(2, 'w').write('x')\n",
        "open('/dev/stdout', 'w').write('x')\n",
    ],
    ids=[
        "buffer-write",
        "from-sys-import-stdout",
        "dunder-stdout",
        "traceback-print-exc",
        "os-write-fd",
        "print-call",
        "print-alias",
        "sys-alias",
        "from-traceback-import",
        "from-os-import-write",
        "getattr-stderr",
        "open-fd",
        "fdopen-fd",
        "dev-stdout",
    ],
)
def test_the_scan_catches_each_bypass(source: str) -> None:
    assert bypasses(source) != []


def test_the_scan_passes_ordinary_code() -> None:
    source = (
        "import subprocess, sys\n"
        "result = subprocess.run(['x'], capture_output=True, text=True)\n"
        "text = result.stderr or result.stdout\n"
        "args = sys.argv\n"
        "with open('notes.txt', 'w') as handle:\n"
        "    handle.write(text)\n"
        "import os\n"
        "os.fdopen(os.open('notes.txt', os.O_WRONLY), 'w').write(text)\n"
    )
    assert bypasses(source) == []


def test_no_module_writes_around_the_choke_point() -> None:
    """Only ``render`` touches stdout or stderr; everything else calls ``emit``."""
    offenders: list[str] = []
    for path in sorted(SRC.rglob("*.py")):
        if path == SRC / "render.py":
            continue
        source = path.read_text(encoding="utf-8")
        offenders += bypasses(source, str(path.relative_to(SRC)))
    assert offenders == []


def test_render_does_not_load_the_database_layer() -> None:
    probe = (
        "import sys, rheo_finance.render\n"
        "loaded = [m for m in sys.modules"
        " if m == 'sqlalchemy' or m.startswith('sqlalchemy.')"
        " or m == 'rheo_finance.storage' or m.startswith('rheo_finance.storage.')]\n"
        "print(len(loaded))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, check=True
    )
    assert result.stdout.strip() == "0"
    assert render.HEADLINE_MAX == 6 and render.STATUS_MAX == 15
