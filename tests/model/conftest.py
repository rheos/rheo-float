"""What the model tests share: a fake ``claude`` executable and synthetic manual files.

The fake is a Python script named ``claude`` in a temporary directory, and ``PATH`` is
set to that directory alone, so no test can reach a real ``claude``. Each run appends
what it saw (its argv, working directory, the directory's entries and stdin) to a
record file beside it, then prints the reply the test set and exits with its code.
"""

from __future__ import annotations

import json
import stat
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel

from rheo_finance.init_data import header_line
from rheo_finance.manual.headers import MANUAL_DIR_NAME, MANUAL_FILES

SCRIPT = """#!{python}
import json, os, sys, time
here = os.path.dirname(os.path.abspath(__file__))
seen = {{
    "program": os.path.basename(sys.argv[0]),
    "argv": sys.argv[1:],
    "cwd": os.getcwd(),
    "entries": sorted(os.listdir(".")),
    "stdin": sys.stdin.buffer.read().decode("utf-8"),
}}
with open(os.path.join(here, "record.jsonl"), "a", encoding="utf-8") as out:
    out.write(json.dumps(seen) + "\\n")
with open(os.path.join(here, "behaviour.json"), encoding="utf-8") as behaviour:
    plan = json.load(behaviour)
time.sleep(plan["sleep"])
sys.stdout.buffer.write(bytes.fromhex(plan["stdout_hex"]))
sys.stderr.write(plan["stderr"])
sys.exit(plan["exit"])
"""


class Verdict(BaseModel):
    """A reply schema for the tests."""

    label: str
    confidence: float


@dataclass
class FakeClaude:
    bin_dir: Path

    def reply(
        self,
        structured: object = None,
        *,
        stdout: str | bytes | None = None,
        stderr: str = "",
        exit_code: int = 0,
        sleep: float = 0.0,
        is_error: bool = False,
    ) -> None:
        """What the next run prints: ``stdout`` as given (bytes go out raw), else a
        CLI JSON reply carrying ``structured``."""
        if stdout is None:
            stdout = json.dumps(
                {"is_error": is_error, "structured_output": structured, "result": ""}
            )
        raw = stdout if isinstance(stdout, bytes) else stdout.encode("utf-8")
        plan = {
            "stdout_hex": raw.hex(),
            "stderr": stderr,
            "exit": exit_code,
            "sleep": sleep,
        }
        (self.bin_dir / "behaviour.json").write_text(json.dumps(plan), encoding="utf-8")

    def runs(self) -> list[dict[str, Any]]:
        record = self.bin_dir / "record.jsonl"
        if not record.exists():
            return []
        lines = record.read_text(encoding="utf-8").splitlines()
        return [json.loads(line) for line in lines]


@pytest.fixture
def fake_claude(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[FakeClaude]:
    bin_dir = tmp_path / "fake-bin"
    bin_dir.mkdir()
    script = bin_dir / "claude"
    script.write_text(SCRIPT.format(python=sys.executable), encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PATH", str(bin_dir))
    fake = FakeClaude(bin_dir)
    fake.reply({"label": "groceries", "confidence": 0.9})
    yield fake


def write_manual(data_dir: Path, name: str, *rows: str) -> None:
    """A synthetic manual file: its header, then ``rows`` as written."""
    manual = data_dir / MANUAL_DIR_NAME
    manual.mkdir(parents=True, exist_ok=True)
    body = header_line(MANUAL_FILES[name]) + "".join(row + "\n" for row in rows)
    (manual / name).write_text(body, encoding="utf-8")
