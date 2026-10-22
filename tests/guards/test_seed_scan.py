"""AC-1, the seed scan: no seed names a prop-firm or a trading account but BloFin.

A seed is anything the build ships that becomes a row: every file ``finance init-data``
writes (the settings template and the manual-entry CSVs, the merchant-rule seed among
them), every file under ``src/rheo_finance/reference/`` once it exists, and the string
literals of every migration revision (a revision could insert seed rows). Each is
scanned for the deny tokens, as words and as two adjacent words joined (so "prop firm"
and "prop-firm" both read as ``propfirm``). A CSV seed with a ``kind`` column fails on a
trading-kind row (``exchange`` and the like) that is not BloFin.

Today it passes because the seeds are empty or header-only.
"""

from __future__ import annotations

import ast
import csv
import io
import re
from collections.abc import Iterable
from pathlib import Path

from rheo_finance import migrations
from rheo_finance.init_data import init_data

from .source_tree import PACKAGE

DENY_TOKENS = frozenset(
    {
        "ftmo",
        "topstep",
        "apex",
        "the5ers",
        "fundednext",
        "propfirm",
        "binance",
        "kucoin",
    }
)
TRADING_KINDS = frozenset({"exchange", "trading", "broker", "brokerage", "prop_firm"})
ALLOWED_TRADING = "blofin"
REFERENCE = PACKAGE / "reference"
VERSIONS = Path(migrations.__file__).resolve().parent / "versions"


def tokens(text: str) -> set[str]:
    words = re.findall(r"[a-z0-9]+", text.lower())
    return set(words) | {a + b for a, b in zip(words, words[1:], strict=False)}


def text_problems(where: str, text: str) -> list[str]:
    return [f"{where}: names {token}" for token in sorted(tokens(text) & DENY_TOKENS)]


def csv_problems(where: str, text: str) -> list[str]:
    reader = csv.DictReader(io.StringIO(text))
    if "kind" not in (reader.fieldnames or []):
        return []
    problems: list[str] = []
    for number, row in enumerate(reader, start=2):
        kind = (row.get("kind") or "").strip().lower()
        named = " ".join(value or "" for value in row.values()).lower()
        if kind in TRADING_KINDS and ALLOWED_TRADING not in named:
            problems.append(
                f"{where}:{number}: a trading account ({kind}) other than BloFin"
            )
    return problems


def file_problems(files: Iterable[tuple[str, Path]]) -> list[str]:
    problems: list[str] = []
    for where, path in files:
        text = path.read_text(encoding="utf-8")
        problems += text_problems(where, text)
        if path.suffix == ".csv":
            problems += csv_problems(where, text)
    return problems


def seed_files(data: Path, reference: Path) -> list[tuple[str, Path]]:
    """What ``init-data`` writes into ``data``, then every file under ``reference``."""
    init_data(data)
    found = [
        (f"init-data {path.relative_to(data)}", path)
        for path in sorted(data.rglob("*"))
        if path.is_file()
    ]
    if reference.is_dir():
        found += [
            (f"reference {path.relative_to(reference)}", path)
            for path in sorted(reference.rglob("*"))
            if path.is_file() and "__pycache__" not in path.parts
        ]
    return found


def revision_problems(versions: Path) -> list[str]:
    problems: list[str] = []
    for path in sorted(versions.glob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                problems += text_problems(f"revision {path.name}", node.value)
    return problems


def test_no_seed_names_a_prop_firm_or_another_trading_account(tmp_path: Path) -> None:
    files = seed_files(tmp_path / "data", REFERENCE)
    # The control: the init-data seeds are read, the merchant-rule seed among them.
    assert any(where.endswith("merchant_rules.csv") for where, _ in files)
    assert file_problems(files) == []


def test_no_revision_seeds_one() -> None:
    assert any(VERSIONS.glob("*.py")), "the control: there are revisions to scan"
    assert revision_problems(VERSIONS) == []


def test_a_planted_seed_row_is_found(tmp_path: Path) -> None:
    reference = tmp_path / "reference"
    reference.mkdir()
    (reference / "merchant_rules.csv").write_text(
        "merchant_key,category\nfunded next,trading_fees\nhydro,utilities\n",
        encoding="utf-8",
    )
    (reference / "accounts.csv").write_text(
        "slug,label,institution,kind\n"
        "blofin,BloFin,BloFin,exchange\n"
        "other,Other,Kraken,exchange\n"
        "cash,Cash,none,wallet\n",
        encoding="utf-8",
    )
    (reference / "vendors.json").write_text(
        '{"groups": ["Prop-Firm fees"]}', encoding="utf-8"
    )
    problems = file_problems(seed_files(tmp_path / "data", reference))
    assert problems == [
        "reference accounts.csv:3: a trading account (exchange) other than BloFin",
        "reference merchant_rules.csv: names fundednext",
        "reference vendors.json: names propfirm",
    ]


def test_a_planted_init_data_row_is_found(tmp_path: Path) -> None:
    data = tmp_path / "data"
    init_data(data)
    rules = data / "manual" / "merchant_rules.csv"
    header = rules.read_text(encoding="utf-8")
    rules.write_text(header + "topstep,trading_fees,,,\n", encoding="utf-8")
    problems = file_problems(seed_files(data, tmp_path / "absent"))
    assert problems == ["init-data manual/merchant_rules.csv: names topstep"]


def test_a_planted_revision_seed_is_found(tmp_path: Path) -> None:
    (tmp_path / "0099_seed.py").write_text(
        "SEED = \"INSERT INTO finance.account (label) VALUES ('KuCoin main')\"\n",
        encoding="utf-8",
    )
    assert revision_problems(tmp_path) == ["revision 0099_seed.py: names kucoin"]
