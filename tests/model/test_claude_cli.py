"""``ClaudeCLIClient`` against a fake ``claude`` (FR-47; AC-48a).

Seam: ``ClaudeCLIClient.call`` launching the fake executable from the conftest, the
only ``claude`` on ``PATH``. It sees exactly the flags of docextract's
``ClaudeCLIExtractor.command`` (read from docextract's source, not imported), the
prompt on stdin, and an empty temporary working directory that is gone afterwards.
A reply that fails the schema is discarded; a failed or missing CLI is
``ModelUnavailable``; neither error quotes what the process printed.
"""

from __future__ import annotations

import ast
import importlib.util
import json
import shutil
from pathlib import Path

import pytest

from conftest import CLAUDE_TRIPWIRE_EXIT, CLAUDE_TRIPWIRE_SCRIPT
from rheo_finance import composition, db
from rheo_finance.contracts import Role
from rheo_finance.manual.accounts import ACCOUNTS_FILE, IDENTIFIERS_FILE
from rheo_finance.model.claude_cli import DEFAULT_TIMEOUT_S, ClaudeCLIClient
from rheo_finance.model.client import ModelReplyInvalid, ModelUnavailable
from rheo_finance.model.guard import PromptGuard, PromptRefused
from rheo_finance.settings import default_settings, parse_settings

from .conftest import FakeClaude, Verdict, write_manual

SYSTEM = "Classify the merchant record."
TEXT = "merchant sample grocer, -45.00 CAD, 2026-03-15"
SCHEMA = json.dumps(Verdict.model_json_schema())
MIRRORED = [
    "-p",
    "--tools", "",
    "--strict-mcp-config",
    "--setting-sources", "",
    "--no-session-persistence",
    "--output-format", "json",
    "--system-prompt", SYSTEM,
    "--json-schema", SCHEMA,
]  # fmt: skip


def client(
    settings_text: str = "", timeout_s: float = DEFAULT_TIMEOUT_S
) -> ClaudeCLIClient:
    settings = parse_settings(settings_text) if settings_text else default_settings()
    return ClaudeCLIClient(PromptGuard(), settings, timeout_s=timeout_s)


def docextract_command() -> list[str]:
    """``ClaudeCLIExtractor.command``'s list, from docextract's source: constants as
    written, the system prompt and the schema as this test's."""
    spec = importlib.util.find_spec("docextract")
    assert spec is not None and spec.origin
    source = (Path(spec.origin).parent / "extractor.py").read_text(encoding="utf-8")
    (extractor,) = [
        node
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.ClassDef) and node.name == "ClaudeCLIExtractor"
    ]
    (command,) = [
        node
        for node in extractor.body
        if isinstance(node, ast.FunctionDef) and node.name == "command"
    ]
    (listed,) = [
        node.value
        for node in command.body
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.List)
    ]
    out: list[str] = []
    for element in listed.elts:
        if isinstance(element, ast.Constant) and isinstance(element.value, str):
            out.append(element.value)
        elif isinstance(element, ast.Name) and element.id == "SYSTEM_PROMPT":
            out.append(SYSTEM)
        elif isinstance(element, ast.Call):  # json.dumps(JSON_SCHEMA)
            out.append(SCHEMA)
        else:
            raise AssertionError(
                f"unexpected element in the command: {ast.dump(element)}"
            )
    return out


def test_the_flags_mirror_docextract_and_the_prompt_goes_on_stdin(
    fake_claude: FakeClaude,
) -> None:
    out = client().call("classify", SYSTEM, TEXT, Verdict)

    assert out == Verdict(label="groceries", confidence=0.9)
    (run,) = fake_claude.runs()
    assert run["program"] == "claude"
    assert run["argv"] == MIRRORED
    assert ["claude", *run["argv"]] == docextract_command()
    assert run["stdin"] == TEXT


def test_it_runs_in_an_empty_temporary_directory_removed_afterwards(
    fake_claude: FakeClaude, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The test's own working directory holds a file the model must not be near.
    monkeypatch.chdir(tmp_path)
    (tmp_path / "statement.txt").write_text("synthetic", encoding="utf-8")

    client().call("classify", SYSTEM, TEXT, Verdict)

    (run,) = fake_claude.runs()
    assert run["entries"] == []
    cwd = Path(run["cwd"])
    assert cwd.name.startswith("finance-model-")
    assert cwd.resolve() != tmp_path.resolve()
    assert not cwd.exists()


# What ``claude`` may receive, written out here so the test pins the allowlist.
ALLOWED_ENV = frozenset(
    {"HOME", "PATH", "USER", "LOGNAME", "SHELL", "TMPDIR", "TERM", "LANG",
     "CLAUDE_CODE_OAUTH_TOKEN"}
)  # fmt: skip
DROPPED_ENV = (
    "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL",
    "ANTHROPIC_MODEL", "OPENAI_API_KEY", "SAMPLE_VENDOR_KEY", "OPENAI_BASE_URL",
    "CLAUDE_CONFIG_DIR", "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX",
    "HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY", "NO_PROXY", "https_proxy",
    "NODE_OPTIONS", "NODE_EXTRA_CA_CERTS", "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY", "FINANCE_OWNER_DSN", "FINANCE_READER_DSN",
    "FINANCE_DATA_DIR", "GITHUB_TOKEN", "PGPASSWORD", "SAMPLE_UNRELATED",
)  # fmt: skip


# macOS sets this one in every new process itself; the test proves the caller's
# value is not what arrives.
OS_SET_ENV = "__CF_USER_TEXT_ENCODING"


def allowed(name: str) -> bool:
    return name in ALLOWED_ENV or name.startswith("LC_") or name == OS_SET_ENV


def test_the_process_gets_only_the_allowlisted_environment(
    fake_claude: FakeClaude, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An exported key would bill a paid API, a base URL or proxy would send the
    prompt elsewhere, and a config directory could pick another login: only what
    ``claude`` needs to find the owner's subscription login reaches it."""
    kept = {
        "HOME": "/sample/home", "USER": "sample-user", "LOGNAME": "sample-user",
        "SHELL": "/bin/zsh", "TMPDIR": str(tmp_path), "TERM": "xterm-256color",
        "LANG": "en_CA.UTF-8", "LC_ALL": "en_CA.UTF-8",
        "CLAUDE_CODE_OAUTH_TOKEN": "sample-subscription-login",
    }  # fmt: skip
    for name in (*DROPPED_ENV, OS_SET_ENV):
        monkeypatch.setenv(name, "sample-value")
    for name, value in kept.items():
        monkeypatch.setenv(name, value)

    client().call("classify", SYSTEM, TEXT, Verdict)

    (run,) = fake_claude.runs()
    env = run["env"]
    assert sorted(name for name in env if not allowed(name)) == []
    assert [name for name in DROPPED_ENV if name in env] == []
    assert env.get(OS_SET_ENV) != "sample-value"
    assert {name: env.get(name) for name in kept} == kept
    assert env["PATH"] == str(fake_claude.bin_dir)


def test_the_tripwire_is_on_path_without_being_asked_for() -> None:
    """The suite's tripwire is autouse: this test does not request it, and the only
    ``claude`` it can find is the tripwire."""
    found = shutil.which("claude")
    assert found is not None
    assert Path(found).parent.name.startswith("claude-tripwire")
    assert Path(found).read_text(encoding="utf-8") == CLAUDE_TRIPWIRE_SCRIPT


def test_outside_the_fake_the_suite_finds_only_the_tripwire(
    claude_tripwire: Path,
) -> None:
    """Without the fake, ``claude`` on PATH is the suite's tripwire, which fails."""
    found = shutil.which("claude")
    assert found is not None and Path(found).parent == claude_tripwire
    with pytest.raises(ModelUnavailable) as caught:
        client().call("classify", SYSTEM, TEXT, Verdict)
    assert str(caught.value) == f"claude -p exited {CLAUDE_TRIPWIRE_EXIT}"


def test_each_stage_takes_its_own_model_setting(fake_claude: FakeClaude) -> None:
    configured = client(
        '[finance.model]\nclassify_model = "sample-model-a"\n'
        'extract_model = "sample-model-b"\n'
    )

    configured.call("classify", SYSTEM, TEXT, Verdict)
    configured.call("suggest", SYSTEM, TEXT, Verdict)
    configured.call("extract", SYSTEM, TEXT, Verdict)

    classify, suggest, extract = (run["argv"] for run in fake_claude.runs())
    assert classify == [*MIRRORED, "--model", "sample-model-a"]
    assert suggest == MIRRORED
    assert extract == [*MIRRORED, "--model", "sample-model-b"]


def test_an_unknown_stage_is_refused_before_claude_runs(
    fake_claude: FakeClaude,
) -> None:
    with pytest.raises(ValueError, match="unknown model stage"):
        client().call("summarize", SYSTEM, TEXT, Verdict)  # type: ignore[arg-type]
    assert fake_claude.runs() == []


@pytest.mark.parametrize(
    "reply",
    [
        {"label": "groceries"},
        {"label": "groceries", "confidence": "high"},
        {"label": ["echoed prompt"], "confidence": 0.9},
    ],
    ids=["missing-field", "wrong-type", "wrong-shape"],
)
def test_an_invalid_reply_is_discarded(fake_claude: FakeClaude, reply: object) -> None:
    fake_claude.reply(reply)
    with pytest.raises(ModelReplyInvalid) as caught:
        client().call("classify", SYSTEM, TEXT, Verdict)
    # Discarded: no reply value and no chained validation error travels with it.
    assert caught.value.__cause__ is None and caught.value.__suppress_context__
    assert str(caught.value) == "the classify reply was invalid and was discarded"
    assert len(fake_claude.runs()) == 1


@pytest.mark.parametrize(
    "stdout",
    ["not json at all", "[1, 2]", json.dumps({"is_error": False, "result": "text"})],
    ids=["not-json", "not-an-object", "no-structured-output"],
)
def test_a_reply_without_structured_output_is_discarded(
    fake_claude: FakeClaude, stdout: str
) -> None:
    fake_claude.reply(stdout=stdout)
    with pytest.raises(ModelReplyInvalid):
        client().call("classify", SYSTEM, TEXT, Verdict)


def test_a_reply_that_is_not_utf8_is_discarded_not_repaired(
    fake_claude: FakeClaude,
) -> None:
    """A stray byte inside a string the schema would accept: decoding it with
    replacement would pass ``label`` through with a replacement character."""
    fake_claude.reply(
        stdout=b'{"is_error": false, "result": "", "structured_output":'
        b' {"label": "gro\xffceries", "confidence": 0.9}}'
    )
    with pytest.raises(ModelReplyInvalid) as caught:
        client().call("classify", SYSTEM, TEXT, Verdict)
    assert caught.value.__cause__ is None and caught.value.__suppress_context__
    assert str(caught.value) == "the classify reply was invalid and was discarded"


def test_a_prompt_and_reply_beyond_ascii_travel_as_utf8(
    fake_claude: FakeClaude, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LC_ALL", "C")
    monkeypatch.setenv("LANG", "C")
    fake_claude.reply({"label": "épicerie", "confidence": 0.9})

    out = client().call("classify", SYSTEM, "marché sample, -45.00 CAD", Verdict)

    assert out.label == "épicerie"
    (run,) = fake_claude.runs()
    assert run["stdin"] == "marché sample, -45.00 CAD"


def test_a_failed_run_is_unavailable_and_quotes_nothing(
    fake_claude: FakeClaude,
) -> None:
    fake_claude.reply(
        stdout="echoed sample grocer", stderr="echoed sample grocer", exit_code=3
    )
    with pytest.raises(ModelUnavailable) as caught:
        client().call("classify", SYSTEM, TEXT, Verdict)
    assert str(caught.value) == "claude -p exited 3"


def test_a_reported_error_is_unavailable(fake_claude: FakeClaude) -> None:
    fake_claude.reply({"label": "groceries", "confidence": 0.9}, is_error=True)
    with pytest.raises(ModelUnavailable, match="reported an error"):
        client().call("classify", SYSTEM, TEXT, Verdict)


def test_a_timeout_is_unavailable(fake_claude: FakeClaude) -> None:
    fake_claude.reply({"label": "groceries", "confidence": 0.9}, sleep=5)
    with pytest.raises(ModelUnavailable, match="timed out"):
        client(timeout_s=0.5).call("classify", SYSTEM, TEXT, Verdict)


def test_no_claude_on_path_is_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    empty = tmp_path / "empty-bin"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    with pytest.raises(ModelUnavailable, match="not on PATH"):
        client().call("classify", SYSTEM, TEXT, Verdict)


# --- the production context -----------------------------------------------------------


def test_the_production_context_guards_with_the_manual_accounts_owner(
    fake_claude: FakeClaude, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``build_context`` wires the ``claude -p`` client behind a guard whose owner
    name comes from the data directory's accounts file."""
    data = tmp_path / "data"
    data.mkdir()
    (data / db.ENV_FILE_NAME).write_text(
        f"{db.OWNER_DSN_KEY}=postgresql://finance_owner:pw@127.0.0.1:1/finance\n"
        f"{db.READER_DSN_KEY}=postgresql://finance_reader:pw@127.0.0.1:1/finance\n",
        encoding="utf-8",
    )
    write_manual(
        data,
        ACCOUNTS_FILE,
        "sample-chequing,Sample chequing,Sample Credit Union,chequing,CAD,monthly,"
        "Testa Ownerly,,,documents,,,,,",
    )
    write_manual(data, IDENTIFIERS_FILE)
    monkeypatch.setenv("FINANCE_DATA_DIR", str(data))
    ctx = composition.build_context(Role.OWNER)
    assert ctx.model_client is not None

    with pytest.raises(PromptRefused) as caught:
        ctx.model_client.call("classify", SYSTEM, "from Testa Ownerly", Verdict)
    assert caught.value.kind == "owner_name"
    assert fake_claude.runs() == []

    ctx.model_client.call("classify", SYSTEM, TEXT, Verdict)
    assert [run["stdin"] for run in fake_claude.runs()] == [TEXT]
