"""What a sink is declared to request, and the digest an approval is pinned to.

A ``SinkSpec`` holds the hosts, the request templates (method, path, allowed query
keys), the key file the sink reads (None for a public one), the payload policy version
and the public data inputs that widen it (a currency set, a sender count and list
digest; never a sender, key or identifier). ``spec_digest`` is sha256 of the canonical
spec. An approval records that digest, so a build that adds a host, a request, a
payload field or a data input makes the approval stale.

The model sorts its tuples on construction, so declaration order never changes the
digest, and refuses duplicates instead of folding them.

``SINK_SPECS`` maps each sink to its provider: a function of the owner's unit of work
that returns the current spec (code constants plus public data inputs read from the
ledger). No provider is registered here yet; a sink's own module registers one.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable
from typing import Annotated, Final

from pydantic import BaseModel, ConfigDict, Field, field_validator

from rheo_finance.context import UnitOfWork

METHODS: Final = frozenset({"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"})
_NAME = re.compile(r"[a-z][a-z0-9_]*")
# A host name, lower case, with an optional port. IPv6 literals are not supported.
_HOST = re.compile(
    r"[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)*(:[0-9]{1,5})?"
)
_LITERAL_SEGMENT = re.compile(r"[A-Za-z0-9._~-]+")
_PARAM_SEGMENT = re.compile(r":[a-z][a-z0-9_]*")
_QUERY_KEY = re.compile(r"[A-Za-z0-9._~-]+")
_PUBLIC_TEXT = re.compile(r"[\x20-\x7e]*")


def _unique_sorted[T: (str, tuple[str, str])](
    values: tuple[T, ...], what: str
) -> tuple[T, ...]:
    if len(set(values)) != len(values):
        raise ValueError(f"{what} has a duplicate")
    return tuple(sorted(values))


class RequestTemplate(BaseModel):
    """One request a sink may make. A path segment ``:name`` matches one segment."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    method: str
    path: str
    query_keys: tuple[str, ...] = ()

    @field_validator("method")
    @classmethod
    def _method(cls, value: str) -> str:
        if value not in METHODS:
            raise ValueError(f"method is one of {', '.join(sorted(METHODS))}")
        return value

    @field_validator("path")
    @classmethod
    def _path(cls, value: str) -> str:
        if not value.startswith("/"):
            raise ValueError("a path starts with /")
        for segment in value[1:].split("/") if value != "/" else ():
            if segment in {".", ".."} or not (
                _LITERAL_SEGMENT.fullmatch(segment) or _PARAM_SEGMENT.fullmatch(segment)
            ):
                raise ValueError("a path segment is a literal or :name")
        return value

    @field_validator("query_keys")
    @classmethod
    def _query_keys(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for key in value:
            if not _QUERY_KEY.fullmatch(key):
                raise ValueError("a query key is letters, digits and ._~-")
        return _unique_sorted(value, "query_keys")

    def as_line(self) -> str:
        """The template as one line of public text, for the ``sink_spec`` table."""
        keys = f" ?{'&'.join(self.query_keys)}" if self.query_keys else ""
        return f"{self.method} {self.path}{keys}"


class SinkSpec(BaseModel):
    """Everything a sink is declared to send or request. Public text only."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    hosts: tuple[str, ...]
    requests: tuple[RequestTemplate, ...]
    key_file: Annotated[str, Field(min_length=1)] | None
    payload_policy_version: Annotated[int, Field(ge=0)]
    data_inputs: tuple[tuple[str, str], ...] = ()

    @field_validator("name")
    @classmethod
    def _name(cls, value: str) -> str:
        if not _NAME.fullmatch(value):
            raise ValueError("a sink name is lower case letters, digits and _")
        return value

    @field_validator("hosts")
    @classmethod
    def _hosts(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for host in value:
            if not _HOST.fullmatch(host):
                raise ValueError("a host is a lower-case name with an optional :port")
        return _unique_sorted(value, "hosts")

    @field_validator("requests")
    @classmethod
    def _requests(
        cls, value: tuple[RequestTemplate, ...]
    ) -> tuple[RequestTemplate, ...]:
        pairs = [(each.method, each.path) for each in value]
        if len(set(pairs)) != len(pairs):
            raise ValueError("requests has a duplicate method and path")
        return tuple(sorted(value, key=lambda each: (each.path, each.method)))

    @field_validator("key_file")
    @classmethod
    def _key_file(cls, value: str | None) -> str | None:
        if value is not None and not _PUBLIC_TEXT.fullmatch(value):
            raise ValueError("key_file is one line of printable text")
        return value

    @field_validator("data_inputs")
    @classmethod
    def _data_inputs(
        cls, value: tuple[tuple[str, str], ...]
    ) -> tuple[tuple[str, str], ...]:
        names = [name for name, _ in value]
        if len(set(names)) != len(names):
            raise ValueError("data_inputs names a key twice")
        for name, text in value:
            if not _NAME.fullmatch(name) or not _PUBLIC_TEXT.fullmatch(text):
                raise ValueError("a data input is a name and one line of public text")
        return _unique_sorted(value, "data_inputs")


def canonical(spec: SinkSpec) -> str:
    """The spec as canonical JSON: sorted keys, no spaces, ASCII only."""
    return json.dumps(
        spec.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )


def spec_digest(spec: SinkSpec) -> str:
    """sha256 hex of the canonical spec, data inputs included."""
    return hashlib.sha256(canonical(spec).encode("ascii")).hexdigest()


SpecProvider = Callable[[UnitOfWork], SinkSpec]

# sink name -> its provider. Empty until a sink's module registers one; tests register
# a test-only provider with monkeypatch.
SINK_SPECS: dict[str, SpecProvider] = {}


def current_spec(uow: UnitOfWork, sink: str) -> SinkSpec | None:
    """The sink's spec from its provider, or None when it has no provider."""
    provider = SINK_SPECS.get(sink)
    if provider is None:
        return None
    spec = provider(uow)
    if spec.name != sink:
        raise ValueError(f"the provider registered for {sink} returned another sink")
    return spec
