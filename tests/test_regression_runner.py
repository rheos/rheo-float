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


STUB_UV = """#!/bin/sh
case "$*" in
  "run ruff check") echo "All checks passed!" ;;
  "run mypy"*) echo "Success: no issues found in 1 source file" ;;
  "run pytest -q") echo "1 passed in 0.01s" ;;
  *) echo "repo check ok: 1 tracked files checked." ;;
esac
"""


def fixture(marker: str) -> str:
    return f'name: fake\ncommand: touch "$ROOT/{marker}"\nexpected: exit 0\n'


def test_runner_runs_only_fixtures_in_the_git_index(tmp_path: Path) -> None:
    """A scratch copy with a stub ``uv``: the tracked fixture runs, the untracked
    one is reported and never run."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env["GIT_CONFIG_GLOBAL"] = os.devnull
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    stub_bin = tmp_path / "bin"
    stub_bin.mkdir()
    (stub_bin / "uv").write_text(STUB_UV)
    (stub_bin / "uv").chmod(0o755)
    env["PATH"] = f"{stub_bin}{os.pathsep}{env.get('PATH', '')}"

    root = tmp_path / "repo"
    regression = root / ".bureau" / "regression"
    regression.mkdir(parents=True)
    runner = regression / "run.sh"
    runner.write_bytes((REPO_ROOT / RUNNER).read_bytes())
    (regression / "01-tracked.md").write_text(fixture("ran-tracked"))
    (regression / "02-staged.md").write_text(fixture("ran-staged"))
    (regression / "03-untracked.md").write_text(fixture("ran-untracked"))

    def git(*args: str) -> None:
        subprocess.run(["git", *args], cwd=root, env=env, check=True)

    git("init", "-q")
    git("add", ".bureau/regression/run.sh", ".bureau/regression/01-tracked.md")
    git("-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-qm", "x")
    git("add", ".bureau/regression/02-staged.md")

    result = subprocess.run(
        ["sh", str(runner)],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    out = result.stdout + result.stderr
    assert result.returncode == 0, out
    assert (root / "ran-tracked").exists()
    assert (root / "ran-staged").exists()
    assert not (root / "ran-untracked").exists()
    assert "SKIP untracked 03-untracked.md" in out.splitlines()
    assert out.splitlines()[-1] == "pass=6 fail=0 skip=1"
