"""``ClaudeCLIClient``: a model step as ``claude -p`` on the owner's subscription.

The only module that starts the model's process (it is in the outbound allowlist).
The flags mirror ``docextract/extractor.py`` ``ClaudeCLIExtractor.command``: no tools,
no MCP servers, no settings files, no saved session, JSON output held to the schema.
``ClaudeCLIExtractor`` itself is not reused, because its prompt and schema are fixed
to one bill. The prompt goes on stdin, and the process runs in an empty temporary
directory, removed afterwards, so nothing on disk is in reach of its working
directory. Each stage takes its model from ``finance.model.<stage>_model``; unset,
the CLI's default applies.

A failure says what failed and never quotes the process's output, which can echo
the prompt.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
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


def model_setting(stage: Stage) -> str:
    return f"finance.model.{stage}_model"


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
                proc = subprocess.run(
                    self.command(program, stage, system, json_schema),
                    input=text,
                    capture_output=True,
                    text=True,
                    cwd=cwd,
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
            reply = json.loads(proc.stdout)
        except json.JSONDecodeError:
            raise ModelReplyInvalid(stage) from None
        if not isinstance(reply, dict):
            raise ModelReplyInvalid(stage)
        if reply.get("is_error"):
            raise ModelUnavailable("claude -p reported an error")
        # Missing or not an object: the schema check discards it.
        return reply.get("structured_output")
