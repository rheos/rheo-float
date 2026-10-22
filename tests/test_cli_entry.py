"""The ``finance`` entry point: help, usage errors, and its import boundary."""

from __future__ import annotations

import subprocess
import sys

import pytest

from rheo_finance.cli.main import HEADLINE, main


def test_help_returns_zero_and_leads_with_headline(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["--help"]) == 0
    out = capsys.readouterr().out
    lines = out.splitlines()
    assert lines[0].startswith("usage: finance")
    assert HEADLINE in out
    assert chr(0x2014) not in out  # no em dash in output


def test_unknown_subcommand_exits_with_usage_error(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["no-such-command"]) == 2
    err = capsys.readouterr().err
    assert "invalid choice" in err


def test_console_script_exit_codes() -> None:
    ok = subprocess.run(
        [sys.executable, "-m", "rheo_finance.cli.main", "--help"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert ok.returncode == 0
    assert HEADLINE in ok.stdout

    bad = subprocess.run(
        [sys.executable, "-m", "rheo_finance.cli.main", "no-such-command"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert bad.returncode == 2


def test_cli_main_does_not_load_the_database_layer() -> None:
    probe = (
        "import sys, rheo_finance.cli.main\n"
        "loaded = [m for m in sys.modules"
        " if m == 'sqlalchemy' or m.startswith('sqlalchemy.')"
        " or m == 'rheo_finance.storage' or m.startswith('rheo_finance.storage.')]\n"
        "print(len(loaded))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.strip() == "0"
