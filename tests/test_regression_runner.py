"""``.bureau/regression/run.sh``, the gate runner the Bureau's integration gate calls.

Running it here would recurse into pytest, so these checks are static: the file is
tracked as an executable POSIX sh script with ``set -eu`` and names the four gates.
"""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
RUNNER = ".bureau/regression/run.sh"
GATES = (
    "uv run ruff check",
    "uv run mypy --strict src tests scripts",
    "uv run pytest -q",
    "uv run python scripts/repo_check.py --tracked",
)


def code_lines() -> list[str]:
    text = (REPO_ROOT / RUNNER).read_text(encoding="utf-8")
    return [line for line in text.splitlines() if not line.lstrip().startswith("#")]


def test_runner_is_an_executable_posix_sh_script() -> None:
    path = REPO_ROOT / RUNNER
    assert path.is_file() and not path.is_symlink()
    assert path.stat().st_mode & stat.S_IXUSR
    assert path.read_text(encoding="utf-8").splitlines()[0] == "#!/bin/sh"
    assert "set -eu" in code_lines()


def test_runner_is_tracked_with_mode_755() -> None:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    out = subprocess.run(
        ["git", "ls-files", "--stage", "--", RUNNER],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert out.split()[:1] == ["100755"], out


def test_runner_names_the_four_gates_in_order() -> None:
    # Each gate is a `run_gate` call whose command is the last line of the call.
    body = "\n".join(code_lines())
    positions = [body.find(f"\n    {gate}\n") for gate in GATES]
    assert all(pos >= 0 for pos in positions), positions
    assert positions == sorted(positions)
