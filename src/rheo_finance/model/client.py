"""The model seam (FR-47; novadiem-engineering section 14): ``ModelClient``.

A feature asks for a model step with ``call(stage, system, text, schema)`` and gets
back an instance of ``schema`` or an exception; it never sees a process, a reply's
raw text or a provider. ``GuardedModelClient.call`` is the single path every client
takes, in this order:

1. the stage is one of ``STAGES`` (each has its own model setting);
2. the prompt guard checks the system prompt and the text, and a refusal raises
   ``PromptRefused`` before anything is sent;
3. the client's ``_send`` delivers the prompt and returns the raw structured reply;
4. the reply is validated by ``schema``; an invalid reply is discarded and raises
   ``ModelReplyInvalid``, which carries neither the reply nor the validation detail
   (both may echo prompt text).

``ClaudeCLIClient`` (``claude -p`` on the owner's subscription) and
``StubModelClient`` (tests) are the clients. There is no SDK and no API key.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Final, Literal, Protocol, final, get_args

from pydantic import BaseModel, ValidationError

from rheo_finance.model.guard import ModelError, PromptGuard

Stage = Literal["classify", "suggest", "extract"]
STAGES: Final[tuple[Stage, ...]] = get_args(Stage)
JsonSchema = dict[str, object]


class ModelUnavailable(ModelError):
    """The model could not be reached: no executable, a failed run or a timeout.
    The ledger and reports keep working on what is already classified."""


class ModelReplyInvalid(ModelError):
    """The reply did not validate against the schema; it was discarded."""

    def __init__(self, stage: Stage) -> None:
        super().__init__(f"the {stage} reply was invalid and was discarded")
        self.stage: Stage = stage


class ModelClient(Protocol):
    """What a feature calls. Every implementation guards before it sends."""

    def call[M: BaseModel](
        self, stage: Stage, system: str, text: str, schema: type[M]
    ) -> M: ...


class GuardedModelClient(ABC):
    """The guard, the send and the validation, in that order, for every client."""

    def __init__(self, guard: PromptGuard) -> None:
        self._guard = guard

    @final
    def call[M: BaseModel](
        self, stage: Stage, system: str, text: str, schema: type[M]
    ) -> M:
        if stage not in STAGES:
            raise ValueError(f"unknown model stage: {stage}")
        self._guard.check(system, text)
        raw = self._send(stage, system, text, schema.model_json_schema())
        try:
            return schema.model_validate(raw)
        except ValidationError:
            # ``from None``: the validation error quotes the reply.
            raise ModelReplyInvalid(stage) from None

    @abstractmethod
    def _send(
        self, stage: Stage, system: str, text: str, json_schema: JsonSchema
    ) -> object:
        """Deliver a prompt the guard admitted; return the raw structured reply."""
