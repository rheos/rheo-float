"""Render and install the primary clone's per-user LaunchAgent."""

from __future__ import annotations

import os
import plistlib
import subprocess
import sys
from pathlib import Path

from rheo_finance.operations import OperationError

LABEL = "ca.rheo.float.tick"
INTERVAL_SECONDS = 900
SYSTEM_PATH = "/usr/bin:/bin:/usr/sbin:/sbin"


def _worktree(path: Path) -> bool:
    return any(
        part == ".bureau" and following == "worktrees"
        for part, following in zip(path.parts, path.parts[1:], strict=False)
    )


def _git(repo: Path, *arguments: str) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(repo), *arguments],
            capture_output=True,
            text=True,
            check=True,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        raise OperationError("install-agent cannot resolve the primary clone") from None
    return result.stdout.strip()


def primary_clone(repo: Path | None = None) -> Path:
    """Resolve shared Git storage, never the calling worktree's checkout root."""
    supplied = repo or Path.cwd()
    prefix = Path(sys.prefix)
    # A worktree caller is safe only with a primary-clone interpreter and no
    # explicit worktree override. The common Git directory still resolves main.
    checked = [prefix, prefix.resolve()]
    if repo is not None:
        checked.extend([repo, repo.resolve()])
    if any(_worktree(path) for path in checked):
        raise OperationError("install-agent refuses a worktree path")
    common = Path(
        _git(supplied, "rev-parse", "--path-format=absolute", "--git-common-dir")
    )
    primary = common.resolve().parent
    if _worktree(primary) or _worktree(common):
        raise OperationError("install-agent refuses a worktree path")
    if repo is not None and repo.resolve() != primary:
        raise OperationError("install-agent --repo must name the primary clone")
    if _git(primary, "symbolic-ref", "--short", "HEAD") != "main":
        raise OperationError("install-agent requires the primary clone on main")
    executable = primary / ".venv" / "bin" / "finance"
    if not executable.is_file() or _worktree(executable.resolve()):
        raise OperationError(
            "install-agent requires the primary clone's finance executable"
        )
    return primary


def render_plist(repo: Path, data: Path) -> str:
    """Render an already validated clone; plistlib escapes paths as XML."""
    binary = repo / ".venv" / "bin"
    return plistlib.dumps(
        {
            "Label": LABEL,
            "ProgramArguments": [str(binary / "finance"), "jobs", "tick"],
            "StartInterval": INTERVAL_SECONDS,
            "RunAtLoad": True,
            "EnvironmentVariables": {
                "PATH": f"{binary}:/usr/local/bin:{SYSTEM_PATH}",
                "FINANCE_DATA_DIR": str(data.expanduser().resolve()),
            },
        },
        fmt=plistlib.FMT_XML,
    ).decode("utf-8")


def install_agent(*, repo: Path | None, data: Path, dry_run: bool) -> str:
    rendered = render_plist(primary_clone(repo), data)
    if dry_run:
        return rendered
    directory = Path.home() / "Library" / "LaunchAgents"
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"{LABEL}.plist"
    target.write_text(rendered, encoding="utf-8")
    try:
        subprocess.run(
            ["launchctl", "bootstrap", f"gui/{os.getuid()}", str(target)],
            check=True,
            capture_output=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        raise OperationError("install-agent bootstrap failed") from None
    return rendered
