"""``StubModelClient`` (AC-9, model part).

Seam: ``call`` on the stub. It records each prompt it receives and returns its
canned reply for the stage, validated like any reply. Image bytes reaching it fail
the run: with the real guard they are refused first; with a guard that admits
everything (standing in for a loosened guard) the stub's own tripwire raises.
"""

from __future__ import annotations

import pytest

from rheo_finance.model.client import ModelClient, ModelReplyInvalid
from rheo_finance.model.guard import PromptGuard, PromptRefused, RefusalKind
from rheo_finance.model.stub import ImageBytesReceived, StubModelClient

from .conftest import Verdict

SYSTEM = "Classify the merchant record."
IMAGES = [
    "\x89PNG\r\n\x1a\n",
    "\xff\xd8\xff\xe0 JFIF",
    "iVBORw0KGgoAAAANSUhEUg",
    "data:image/png;base64,iVBOR",
    "scan /9j/4AAQSkZJRgABAQ",
    "GIF89a",
]


class AdmitAll(PromptGuard):
    """A guard that admits every prompt: what the stub's tripwire is for."""

    def refusal(self, *texts: str) -> RefusalKind | None:
        return None


def test_it_records_each_prompt_and_returns_the_stage_reply() -> None:
    stub = StubModelClient(
        {
            "classify": {"label": "groceries", "confidence": 0.9},
            "suggest": {"label": "cancel", "confidence": 0.5},
        }
    )
    client: ModelClient = stub

    first = client.call("classify", SYSTEM, "merchant sample grocer", Verdict)
    second = client.call("suggest", "Suggest.", "sample streaming, monthly", Verdict)

    assert (first.label, second.label) == ("groceries", "cancel")
    assert [(p.stage, p.system, p.text) for p in stub.prompts] == [
        ("classify", SYSTEM, "merchant sample grocer"),
        ("suggest", "Suggest.", "sample streaming, monthly"),
    ]
    assert stub.prompts[0].json_schema == Verdict.model_json_schema()


def test_an_invalid_canned_reply_is_discarded() -> None:
    stub = StubModelClient({"classify": {"label": "groceries"}})
    with pytest.raises(ModelReplyInvalid):
        stub.call("classify", SYSTEM, "merchant sample grocer", Verdict)


@pytest.mark.parametrize("image", IMAGES)
def test_image_bytes_are_refused_by_the_guard_before_the_stub(image: str) -> None:
    stub = StubModelClient({"classify": {"label": "x", "confidence": 1}})
    with pytest.raises(PromptRefused):
        stub.call("classify", SYSTEM, f"receipt {image}", Verdict)
    assert stub.prompts == []


@pytest.mark.parametrize("image", IMAGES)
def test_image_bytes_that_pass_a_loosened_guard_fail_the_run(image: str) -> None:
    stub = StubModelClient(
        {"classify": {"label": "x", "confidence": 1}}, guard=AdmitAll()
    )
    with pytest.raises(ImageBytesReceived):
        stub.call("classify", SYSTEM, f"receipt {image}", Verdict)
    # The control: the loosened guard does admit an ordinary prompt.
    assert stub.call("classify", SYSTEM, "receipt", Verdict).label == "x"


def test_image_bytes_in_the_system_prompt_fail_the_run() -> None:
    stub = StubModelClient(
        {"classify": {"label": "x", "confidence": 1}}, guard=AdmitAll()
    )
    with pytest.raises(ImageBytesReceived):
        stub.call("classify", f"{SYSTEM} {IMAGES[0]}", "receipt", Verdict)
