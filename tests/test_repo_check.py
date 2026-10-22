"""``scripts/repo_check.py`` as a command (AC-46): exit code and output per rule.

Each test builds a throwaway git repo, stages files in it and runs the real script.
Every number below is made up; the card-shaped ones start with 999 or are built to be
Luhn-valid only so the card rule has something to catch.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "repo_check.py"

MANIFEST = "tests/fixtures/synthetic/MANIFEST.toml"
FIXTURE = "tests/fixtures/synthetic/layout.txt"

# Luhn-valid 16-digit numbers. The 999-prefixed one passes the digit-run rule, so only
# the card rule can catch it; the grouped ones have no run of 8 digits at all.
LUHN_999 = "9990000000000003"
LUHN_GROUPED_SPACES = "9991 2345 6789 0121"
LUHN_GROUPED_HYPHENS = "9991-2345-6789-0121"
NOT_LUHN_GROUPED = "9991 2345 6789 0122"
PLAIN_RUN = "12345678"  # neither a date nor 999-prefixed


def git_env() -> dict[str, str]:
    """An environment free of the caller's git config and any hook-time GIT_* vars."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env["GIT_CONFIG_GLOBAL"] = os.devnull
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    return env


class Repo:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.git("init", "-q")

    def git(self, *args: str) -> None:
        subprocess.run(["git", *args], cwd=self.root, env=git_env(), check=True)

    def stage(self, path: str, content: str) -> None:
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
        self.git("add", "-f", path)

    def stage_bytes(self, path: str, content: bytes) -> None:
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        self.git("add", "-f", path)

    def manifest(self, *paths: str) -> None:
        body = "".join(
            f'[[fixture]]\npath = "{p}"\nsynthetic = true\ndescription = "fake"\n\n'
            for p in paths
        )
        self.stage(MANIFEST, body)

    def commit(self) -> None:
        self.git(
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@example.invalid",
            "commit",
            "-q",
            "--no-verify",
            "-m",
            "fixture",
        )

    def check(
        self, mode: str = "--staged", subdir: str = ""
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(SCRIPT), mode],
            cwd=self.root / subdir,
            env=git_env(),
            capture_output=True,
            text=True,
            check=False,
        )


@pytest.fixture
def repo(tmp_path: Path) -> Repo:
    return Repo(tmp_path)


def output(result: subprocess.CompletedProcess[str]) -> str:
    return result.stdout + result.stderr


def assert_fails(
    result: subprocess.CompletedProcess[str], where: str, rule_word: str, *secrets: str
) -> None:
    out = output(result)
    assert result.returncode == 1, out
    assert out.startswith("repo check failed:")
    failing = [line for line in out.splitlines() if line.strip().startswith(where)]
    assert failing, out
    assert any(rule_word in line for line in failing), out
    for secret in secrets:
        # Neither the number nor its digits without separators is ever echoed.
        assert secret not in out
        assert secret.replace(" ", "").replace("-", "") not in out


def assert_passes(result: subprocess.CompletedProcess[str]) -> None:
    assert result.returncode == 0, output(result)
    assert output(result).startswith("repo check ok:")


# Rule 1: statement-type files and the fixture manifest.


def test_staged_statement_pdf_outside_synthetic_fails(repo: Repo) -> None:
    repo.stage("docs/statement.pdf", "%PDF-1.4 fake\n")
    assert_fails(repo.check(), "docs/statement.pdf", "statement-type")


def test_staged_classify_golden_csv_fails(repo: Repo) -> None:
    repo.stage("classify_golden.csv", "date,amount\n20260131,1.00\n")
    assert_fails(repo.check(), "classify_golden.csv", "statement-type")


@pytest.mark.parametrize(
    "path",
    [
        "export.eml",
        "ledger.dump",
        "ledger.sql",
        "a.qfx",
        "a.ofx",
        "rows.jsonl",
        "scan.heic",
        "scan.JPG",
        "data/export.json",
        "tests/fixtures/loose.csv",
        # Beyond the spec's list on purpose.
        "photo.jpeg",
        "shot.png",
        "book.xlsx",
        "book.xls",
        "bundle.zip",
    ],
)
def test_every_statement_extension_outside_synthetic_fails(
    repo: Repo, path: str
) -> None:
    repo.stage(path, "fake\n")
    assert_fails(repo.check(), path, "statement-type")


def test_json_under_reference_is_not_statement_type(repo: Repo) -> None:
    repo.stage("src/rheo_finance/reference/codes.json", '{"code": "A1"}\n')
    assert_passes(repo.check())


def test_listed_synthetic_csv_passes(repo: Repo) -> None:
    repo.manifest("tests/fixtures/synthetic/rows.csv")
    repo.stage("tests/fixtures/synthetic/rows.csv", "date,ref\n20260131,99912345678\n")
    assert_passes(repo.check())


def test_fixture_missing_from_manifest_fails(repo: Repo) -> None:
    repo.manifest(FIXTURE)
    repo.stage(FIXTURE, "fake layout\n")
    repo.stage("tests/fixtures/synthetic/extra.txt", "fake layout\n")
    assert_fails(repo.check(), "tests/fixtures/synthetic/extra.txt", "MANIFEST")


def test_fixture_with_no_manifest_fails(repo: Repo) -> None:
    repo.stage(FIXTURE, "fake layout\n")
    assert_fails(repo.check(), FIXTURE, "MANIFEST")


def test_manifest_entry_not_marked_synthetic_does_not_list(repo: Repo) -> None:
    repo.stage(MANIFEST, f'[[fixture]]\npath = "{FIXTURE}"\ndescription = "fake"\n')
    repo.stage(FIXTURE, "fake layout\n")
    assert_fails(repo.check(), FIXTURE, "MANIFEST")


def test_unparseable_manifest_fails(repo: Repo) -> None:
    repo.stage(MANIFEST, "[[fixture]\npath = \n")
    assert_fails(repo.check(), MANIFEST, "does not parse")


def test_dropping_a_manifest_entry_fails_even_if_fixture_unchanged(repo: Repo) -> None:
    repo.manifest(FIXTURE)
    repo.stage(FIXTURE, "fake layout\n")
    repo.commit()
    repo.manifest()  # stage a manifest with no entries; the fixture is not restaged
    assert_fails(repo.check(), FIXTURE, "MANIFEST")


# Rule 2: digit runs and card numbers in data-shaped files.


def test_luhn_valid_16_digit_number_in_fixture_fails(repo: Repo) -> None:
    repo.manifest(FIXTURE)
    repo.stage(FIXTURE, f"header\ncard {LUHN_999} end\n")
    result = repo.check()
    assert_fails(result, f"{FIXTURE}:2", "Luhn", LUHN_999)


@pytest.mark.parametrize("number", [LUHN_GROUPED_SPACES, LUHN_GROUPED_HYPHENS])
def test_luhn_valid_number_with_separators_fails(repo: Repo, number: str) -> None:
    repo.manifest(FIXTURE)
    repo.stage(FIXTURE, f"card: {number}\n")
    assert_fails(repo.check(), f"{FIXTURE}:1", "Luhn", number)


@pytest.mark.parametrize(
    "line",
    [
        f"ref 123 {LUHN_GROUPED_SPACES}",  # a neighbouring group before
        f"{LUHN_GROUPED_SPACES} 123 end",  # a neighbouring group after
        f"id 4567 {LUHN_GROUPED_HYPHENS}-890",  # neighbours on both sides
        "card 9991-2345 6789-0121",  # mixed separators
        "card 9991  2345  6789  0121",  # double spaces
        "card 9991 - 2345 - 6789 - 0121",  # spaced hyphens
    ],
)
def test_grouped_card_with_neighbours_or_odd_separators_fails(
    repo: Repo, line: str
) -> None:
    repo.manifest(FIXTURE)
    repo.stage(FIXTURE, f"{line}\n")
    assert_fails(repo.check(), f"{FIXTURE}:1", "Luhn", LUHN_GROUPED_SPACES)


def test_grouped_number_that_fails_luhn_passes(repo: Repo) -> None:
    repo.manifest(FIXTURE)
    repo.stage(FIXTURE, f"ref: {NOT_LUHN_GROUPED}\n")
    assert_passes(repo.check())


def test_date_and_millisecond_epoch_pass(repo: Repo) -> None:
    # 1716000000000 is Luhn-valid, so this also pins that the epoch shape is never
    # Luhn-checked as a card.
    repo.manifest(FIXTURE)
    repo.stage(FIXTURE, "date,timestamp\n20260131,1716000000000\n")
    assert_passes(repo.check())


def test_columns_of_iso_dates_pass(repo: Repo) -> None:
    repo.manifest(FIXTURE)
    repo.stage(FIXTURE, "2026-01-31 2026-02-01 2026-02-02\n20260131 20260201\n")
    assert_passes(repo.check())


@pytest.mark.parametrize("run", [PLAIN_RUN, "20261332", "20260230", "18991231"])
def test_eight_digit_run_not_date_nor_999_fails(repo: Repo, run: str) -> None:
    repo.manifest(FIXTURE)
    repo.stage(FIXTURE, f"line one\nref={run};\n")
    assert_fails(repo.check(), f"{FIXTURE}:2", "8 or more digits", run)


@pytest.mark.parametrize("codec", ["utf-16", "utf-16-le", "utf-16-be", "utf-32"])
def test_wide_encoded_fixture_is_decoded_and_scanned(repo: Repo, codec: str) -> None:
    # "utf-16" and "utf-32" write a BOM; the -le and -be forms have none, so only the
    # NUL-interleave pattern gives them away.
    repo.manifest(FIXTURE)
    repo.stage_bytes(FIXTURE, f"header\nref {PLAIN_RUN}\n".encode(codec))
    assert_fails(repo.check(), f"{FIXTURE}:2", "8 or more digits", PLAIN_RUN)


def test_utf16_bom_decodes_text_the_interleave_check_would_miss(repo: Repo) -> None:
    # Mostly non-ASCII text has few NUL bytes in UTF-16, so only the BOM identifies it.
    repo.manifest(FIXTURE)
    text = "\u6578" * 40 + f" {PLAIN_RUN}\n"
    repo.stage_bytes(FIXTURE, text.encode("utf-16"))
    assert_fails(repo.check(), f"{FIXTURE}:1", "8 or more digits", PLAIN_RUN)


def test_wide_encoded_card_fails(repo: Repo) -> None:
    repo.manifest(FIXTURE)
    repo.stage_bytes(FIXTURE, f"card {LUHN_GROUPED_SPACES}\n".encode("utf-16"))
    assert_fails(repo.check(), f"{FIXTURE}:1", "Luhn", LUHN_GROUPED_SPACES)


def test_clean_utf16_fixture_passes(repo: Repo) -> None:
    repo.manifest(FIXTURE)
    repo.stage_bytes(FIXTURE, "date 20260131 ref 99912345678\n".encode("utf-16"))
    assert_passes(repo.check())


def test_data_file_with_nul_bytes_that_does_not_decode_fails(repo: Repo) -> None:
    repo.manifest(FIXTURE)
    repo.stage_bytes(FIXTURE, b"\x00\x01\x02 1234\x005678\x00 9012\x00\x00\xff")
    result = repo.check()
    assert_fails(result, FIXTURE, "does not decode")
    # Path and rule only: no line number after the path.
    assert re.search(re.escape(FIXTURE) + r":[0-9]", output(result)) is None


def test_999_prefixed_run_passes(repo: Repo) -> None:
    repo.manifest(FIXTURE)
    repo.stage(FIXTURE, "account 99912345678 ref 9990000000000001234\n")
    assert_passes(repo.check())


def test_digits_inside_an_alphanumeric_token_pass(repo: Repo) -> None:
    repo.manifest(FIXTURE)
    repo.stage(FIXTURE, "sha a1234567890123456789b and id x87654321\n")
    assert_passes(repo.check())


def test_data_shaped_txt_outside_fixtures_is_scanned(repo: Repo) -> None:
    repo.stage("notes/export.txt", f"value {PLAIN_RUN}\n")
    assert_fails(repo.check(), "notes/export.txt:1", "8 or more digits", PLAIN_RUN)


def test_reference_json_is_scanned(repo: Repo) -> None:
    repo.stage("src/rheo_finance/reference/codes.json", f'{{"id": "{PLAIN_RUN}"}}\n')
    result = repo.check()
    assert_fails(
        result, "src/rheo_finance/reference/codes.json:1", "8 or more", PLAIN_RUN
    )


def test_non_data_files_with_long_digit_runs_pass(repo: Repo) -> None:
    run = "98765432109876543210"
    repo.stage("uv.lock", f'sha256 = "{run}"\nsize = {PLAIN_RUN}\n')
    repo.stage("pyproject.toml", f'version = "{run}"\n')
    repo.stage(".claude/settings.json", f'{{"n": {PLAIN_RUN}, "c": "{LUHN_999}"}}\n')
    repo.stage(
        "src/rheo_finance/storage/migrations/versions/0001_init.py",
        f'revision = "{PLAIN_RUN}"\ndown = {run}\n',
    )
    repo.stage("README.md", f"see {PLAIN_RUN}\n")
    assert_passes(repo.check())


# Modes.


def test_tracked_mode_checks_committed_files(repo: Repo) -> None:
    repo.stage("notes/export.txt", f"value {PLAIN_RUN}\n")
    repo.commit()
    assert_passes(repo.check("--staged"))  # nothing staged now
    assert_fails(repo.check("--tracked"), "notes/export.txt:1", "8 or more", PLAIN_RUN)


def test_staged_mode_ignores_unstaged_working_tree_content(repo: Repo) -> None:
    repo.stage("notes/export.txt", "clean\n")
    (repo.root / "notes" / "export.txt").write_text(f"value {PLAIN_RUN}\n")
    (repo.root / "loose.pdf").write_text("untracked\n")
    assert_passes(repo.check())


@pytest.mark.parametrize("mode", ["--staged", "--tracked"])
def test_run_from_a_subdirectory_checks_the_whole_repo(repo: Repo, mode: str) -> None:
    repo.stage("src/app.py", "x = 1\n")
    repo.stage("docs/statement.pdf", "fake\n")
    repo.stage(FIXTURE, "fake layout\n")  # not in any manifest
    result = repo.check(mode, subdir="src")
    assert_fails(result, "docs/statement.pdf", "statement-type")
    assert_fails(result, FIXTURE, "MANIFEST")


def test_clean_repo_passes_in_both_modes(repo: Repo) -> None:
    repo.manifest(FIXTURE)
    repo.stage(FIXTURE, "fake layout, account 99900012345, 20260131\n")
    repo.stage("src/app.py", "x = 1\n")
    assert_passes(repo.check("--staged"))
    repo.commit()
    assert_passes(repo.check("--tracked"))


def test_a_mode_is_required(repo: Repo) -> None:
    result = subprocess.run(
        [sys.executable, str(SCRIPT)],
        cwd=repo.root,
        env=git_env(),
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 2
