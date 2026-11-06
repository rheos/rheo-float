"""Synthetic Git clones only; no real LaunchAgent is installed."""

from __future__ import annotations

import plistlib
import subprocess
import sys
from pathlib import Path

import pytest

from rheo_finance.cli.main import main


def clone(tmp_path: Path) -> Path:
    repo = tmp_path / "primary & clone"
    repo.mkdir()
    subprocess.run(
        ["git", "init", "-b", "main", str(repo)], check=True, capture_output=True
    )
    binary = repo / ".venv" / "bin" / "finance"
    binary.parent.mkdir(parents=True)
    binary.write_text("synthetic executable")
    return repo


def test_public_install_agent_dry_run_pins_primary_paths_without_installing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = clone(tmp_path)
    data = tmp_path / "data & local"
    home = tmp_path / "synthetic-home"
    monkeypatch.setattr(sys, "prefix", str(tmp_path / "prefix"))
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.setenv("FINANCE_DATA_DIR", str(data))
    calls: list[list[str]] = []
    real = subprocess.run

    def guarded(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        assert argv[0] == "git", "dry-run must never invoke launchctl"
        calls.append(argv)
        # Typed subprocess kwargs are forwarded unchanged to the real local git.
        return real(argv, **kwargs)  # type: ignore[call-overload,no-any-return]

    monkeypatch.setattr(subprocess, "run", guarded)
    assert main(["jobs", "install-agent", "--repo", str(repo), "--dry-run"]) == 0
    out = capsys.readouterr()
    assert out.err == ""
    plist = plistlib.loads(out.out.encode())
    assert plist["StartInterval"] == 900
    assert plist["RunAtLoad"] is True
    assert plist["ProgramArguments"] == [
        str(repo / ".venv/bin/finance"),
        "jobs",
        "tick",
    ]
    assert plist["EnvironmentVariables"] == {
        "PATH": f"{repo}/.venv/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
        "FINANCE_DATA_DIR": str(data.resolve()),
    }
    assert any("--git-common-dir" in call for call in calls)
    assert not home.exists() and not data.exists()


@pytest.mark.parametrize(
    "refusal", ["repo", "prefix", "common", "branch", "missing", "symlink"]
)
def test_public_install_agent_refuses_unsafe_installations(
    refusal: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo = clone(tmp_path)
    monkeypatch.setattr(sys, "prefix", str(tmp_path / "prefix"))
    requested = repo
    if refusal == "repo":
        requested = tmp_path / ".bureau/worktrees/feature"
    elif refusal == "prefix":
        monkeypatch.setattr(
            sys, "prefix", str(tmp_path / ".bureau/worktrees/feature/.venv")
        )
    elif refusal in {"common", "symlink"}:
        moved = tmp_path / ".bureau/worktrees/feature"
        moved.parent.mkdir(parents=True)
        repo.rename(moved)
        requested = moved
        if refusal == "symlink":
            requested = tmp_path / "innocent-alias"
            requested.symlink_to(moved, target_is_directory=True)
        else:
            monkeypatch.chdir(moved)
    elif refusal == "branch":
        subprocess.run(
            ["git", "-C", str(repo), "symbolic-ref", "HEAD", "refs/heads/feature"],
            check=True,
        )
    else:
        (repo / ".venv/bin/finance").unlink()
    args = ["jobs", "install-agent", "--dry-run"]
    if refusal != "common":
        args.extend(["--repo", str(requested)])
    assert main(args) == 1
    out = capsys.readouterr()
    assert out.out == ""
    assert "install-agent" in out.err and "Traceback" not in out.err
    if refusal in {"repo", "prefix", "common", "symlink"}:
        assert "refuses a worktree path" in out.err
    elif refusal == "branch":
        assert "requires the primary clone on main" in out.err
    else:
        assert "requires the primary clone's finance executable" in out.err


def test_public_dry_run_resolves_common_primary_from_linked_checkout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = clone(tmp_path)
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "commit",
            "--allow-empty",
            "-m",
            "Synthetic",
        ],
        check=True,
        capture_output=True,
    )
    linked = tmp_path / "linked"
    subprocess.run(
        ["git", "-C", str(repo), "worktree", "add", "-b", "feature", str(linked)],
        check=True,
        capture_output=True,
    )
    monkeypatch.setattr(sys, "prefix", str(tmp_path / "prefix"))
    monkeypatch.chdir(linked)
    monkeypatch.setenv("FINANCE_DATA_DIR", str(tmp_path / "data"))
    assert main(["jobs", "install-agent", "--dry-run"]) == 0
    plist = plistlib.loads(capsys.readouterr().out.encode())
    assert plist["ProgramArguments"][0] == str(repo / ".venv/bin/finance")
    assert str(linked) not in repr(plist)
    assert main(["jobs", "install-agent", "--repo", str(linked), "--dry-run"]) == 1
    assert "--repo must name the primary clone" in capsys.readouterr().err
