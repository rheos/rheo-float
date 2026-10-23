"""``ClaudeCLIClient``: a model step as ``claude -p`` on the owner's subscription.

The only module that starts the model's process (it is in the outbound allowlist).
The flags mirror ``docextract/extractor.py`` ``ClaudeCLIExtractor.command``: no tools,
no MCP servers, no settings files, no saved session, JSON output held to the schema.
``ClaudeCLIExtractor`` itself is not reused, because its prompt and schema are fixed
to one bill. The prompt goes on stdin, and the process runs in an empty temporary
directory, removed afterwards, so nothing on disk is in reach of its working
directory. Its environment is an allowlist (``model_environment``): home, path,
user, shell, temporary directory, terminal, locale and the subscription's login
token, nothing else, so the call runs on the owner's subscription login and
nowhere else. Each stage takes its model from
``finance.model.<stage>_model``; unset, the CLI's default applies.

A failure says what failed and never quotes the process's output, which can echo
the prompt. The prompt is sent and the reply read as UTF-8 bytes whatever the
locale; a reply that is not valid UTF-8 is discarded, never repaired.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from collections.abc import Mapping
from typing import Final

from rheo_finance.model.client import (
    GuardedModelClient,
    JsonSchema,
    ModelReplyInvalid,
    ModelUnavailable,
    Stage,
)
from rheo_finance.model.guard import PromptGuard
from rheo_finance.settings import Settings

EXECUTABLE: Final = "claude"
DEFAULT_TIMEOUT_S: Final = 300.0
ENCODING: Final = "utf-8"

# The only variables the process gets: what ``claude`` needs to find the owner's
# subscription login, and the subscription's own headless login token. Everything
# else is dropped: a provider key or token would bill a paid API, a base URL, proxy
# or provider switch would send the prompt elsewhere, a config directory could
# select another login, Node options could load code, and the ledger's DSNs and
# credentials are none of its business.
KEPT_NAMES: Final = frozenset(
    {"HOME", "PATH", "USER", "LOGNAME", "SHELL", "TMPDIR", "TERM", "LANG",
     "CLAUDE_CODE_OAUTH_TOKEN"}
)  # fmt: skip
KEPT_PREFIX: Final = "LC_"


def model_setting(stage: Stage) -> str:
    return f"finance.model.{stage}_model"


def model_environment(environ: Mapping[str, str]) -> dict[str, str]:
    """The variables of ``environ`` that ``KEPT_NAMES`` names or that start with
    ``LC_`` (the locale); nothing else."""
    return {
        name: value
        for name, value in environ.items()
        if name in KEPT_NAMES or name.startswith(KEPT_PREFIX)
    }


class ClaudeCLIClient(GuardedModelClient):
    def __init__(
        self,
        guard: PromptGuard,
        settings: Settings,
        *,
        executable: str = EXECUTABLE,
        timeout_s: float = DEFAULT_TIMEOUT_S,
    ) -> None:
        super().__init__(guard)
        self._settings = settings
        self._executable = executable
        self._timeout_s = timeout_s

    def command(
        self, program: str, stage: Stage, system: str, json_schema: JsonSchema
    ) -> list[str]:
        cmd = [
            program, "-p",
            "--tools", "",
            "--strict-mcp-config",
            "--setting-sources", "",
            "--no-session-persistence",
            "--output-format", "json",
            "--system-prompt", system,
            "--json-schema", json.dumps(json_schema),
        ]  # fmt: skip
        model = self._settings.get(model_setting(stage))
        if isinstance(model, str) and model:
            cmd += ["--model", model]
        return cmd

    def _send(
        self, stage: Stage, system: str, text: str, json_schema: JsonSchema
    ) -> object:
        program = shutil.which(self._executable)
        if program is None:
            raise ModelUnavailable("the claude CLI is not on PATH")
        with tempfile.TemporaryDirectory(prefix="finance-model-") as cwd:
            try:
                # Bytes, not text mode: the locale's encoding never applies, and
                # the guard has refused lone surrogates, so this encode holds.
                proc = subprocess.run(
                    self.command(program, stage, system, json_schema),
                    input=text.encode(ENCODING),
                    capture_output=True,
                    cwd=cwd,
                    env=model_environment(os.environ),
                    timeout=self._timeout_s,
                    check=False,
                )
            except subprocess.TimeoutExpired:
                raise ModelUnavailable(
                    f"claude -p timed out after {self._timeout_s:g}s"
                ) from None
            except OSError:
                raise ModelUnavailable("claude -p could not be started") from None
        if proc.returncode != 0:
            raise ModelUnavailable(f"claude -p exited {proc.returncode}")
        try:
            # Strict: a byte that is not UTF-8 discards the reply. A replacement
            # character could otherwise pass the schema as part of a string.
            reply = json.loads(proc.stdout.decode(ENCODING))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ModelReplyInvalid(stage) from None
        if not isinstance(reply, dict):
            raise ModelReplyInvalid(stage)
        if reply.get("is_error"):
            raise ModelUnavailable("claude -p reported an error")
        # Missing or not an object: the schema check discards it.
        return reply.get("structured_output")
