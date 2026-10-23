"""``StubModelClient``: the model client tests pass (AC-9, model part).

It records every prompt that reaches it and returns a canned reply per stage. The
guard runs first, as for every client, so a refused prompt is never recorded. Image
bytes reaching the stub fail the run (``ImageBytesReceived`` is an
``AssertionError``, which no model-step handler catches): it is the tripwire behind
the guard's image class, set even if that class were ever loosened.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from rheo_finance.model.client import GuardedModelClient, JsonSchema, Stage
from rheo_finance.model.guard import PromptGuard, holds_image


class ImageBytesReceived(AssertionError):
    """A prompt with image bytes reached the stub."""


@dataclass(frozen=True)
class RecordedPrompt:
    stage: Stage
    system: str
    text: str
    json_schema: JsonSchema


class StubModelClient(GuardedModelClient):
    def __init__(
        self,
        replies: Mapping[Stage, object],
        *,
        guard: PromptGuard | None = None,
    ) -> None:
        """``replies`` holds the raw reply each stage returns, valid or not."""
        super().__init__(guard if guard is not None else PromptGuard())
        self._replies = dict(replies)
        self.prompts: list[RecordedPrompt] = []

    def _send(
        self, stage: Stage, system: str, text: str, json_schema: JsonSchema
    ) -> object:
        self.prompts.append(RecordedPrompt(stage, system, text, json_schema))
        if holds_image(system) or holds_image(text):
            raise ImageBytesReceived(f"a {stage} prompt held image bytes")
        if stage not in self._replies:
            raise AssertionError(f"the stub has no {stage} reply")
        return self._replies[stage]
