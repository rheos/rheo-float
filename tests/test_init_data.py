"""``finance init-data``: the data-directory tree, and that a rerun keeps edits.

Every test points ``FINANCE_DATA_DIR`` at a temporary directory; none touches the real
data directory.
"""

from __future__ import annotations

import stat
from collections.abc import Iterator
from pathlib import Path

import pytest

from rheo_finance.cli.main import main
from rheo_finance.manual.headers import MANUAL_FILES
from rheo_finance.settings import load_settings

PRIVATE_DIRS = ("mail-inbox/eml", "mail-inbox/drops", "dumps")
OTHER_DIRS = ("staging", "packs", "logs", "manual")


@pytest.fixture
def data(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    target = tmp_path / "Finances"
    monkeypatch.setenv("FINANCE_DATA_DIR", str(target))
    yield target


def mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_init_data_creates_the_tree(
    data: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["init-data"]) == 0
    out = capsys.readouterr().out
    assert out.splitlines()[0].startswith("finance init-data: created ")
    assert chr(0x2014) not in out

    assert mode(data) == 0o700
    for name in PRIVATE_DIRS:
        assert (data / name).is_dir() and mode(data / name) == 0o700, name
    for name in OTHER_DIRS:
        assert (data / name).is_dir(), name

    settings = data / "settings.toml"
    assert settings.read_text(encoding="utf-8").startswith("# ")
    assert load_settings(settings).values == load_settings(data / "none.toml").values

    assert "payees.csv" in MANUAL_FILES and "classify_golden.csv" in MANUAL_FILES
    written = sorted(path.name for path in (data / "manual").iterdir())
    assert written == sorted(MANUAL_FILES)
    for name, columns in MANUAL_FILES.items():
        text = (data / "manual" / name).read_text(encoding="utf-8")
        assert text == ",".join(columns) + "\n", name


def test_a_second_run_leaves_edited_files_untouched(
    data: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["init-data"]) == 0
    settings = data / "settings.toml"
    payees = data / "manual" / "payees.csv"
    settings.write_text("finance.owed.max_rows = 9\n", encoding="utf-8")
    payees.write_text("slug,label\nexample,Example\n", encoding="utf-8")
    capsys.readouterr()

    assert main(["init-data"]) == 0
    out = capsys.readouterr().out
    assert "is already set up" in out.splitlines()[0]
    assert settings.read_text(encoding="utf-8") == "finance.owed.max_rows = 9\n"
    assert payees.read_text(encoding="utf-8") == "slug,label\nexample,Example\n"


def test_an_existing_data_dir_keeps_its_files_and_gains_the_rest(data: Path) -> None:
    data.mkdir(mode=0o755)
    data.chmod(0o755)
    (data / "db.env").write_text("KEY=value\n", encoding="utf-8")
    assert main(["init-data"]) == 0
    assert (data / "db.env").read_text(encoding="utf-8") == "KEY=value\n"
    assert mode(data) == 0o700
    assert (data / "manual" / "accounts.csv").is_file()
