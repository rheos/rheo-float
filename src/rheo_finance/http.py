"""The one HTTP choke point (FR-12, FR-44, FR-65): every HTTP sink goes through
``Client``.

``Client.request`` checks, in this order and all before any socket: the sink gate
(``SinkDisabled``); the URL's scheme and host against the sink's ``SinkSpec.hosts``
(``HostRefused``); then the method, path and query keys against the client's allowlist
(``AllowlistRefused``). The client's spec must also be the spec the approval covers, so
a caller cannot widen a sink by handing in a spec of its own.

The opener is built by hand from stdlib urllib with only the HTTPS handler (plus HTTP
when a test allows it): no proxy handler, so an environment proxy cannot reroute a
request, and no redirect handler, so a redirect is never followed. A 3xx that points
at another origin raises ``HostRefused``; one on the same origin is returned to the
caller as it came. Any other status is returned too.

Errors never carry a header value or a query value: the client builds its own
messages, scrubs any text it quotes, and raises without chaining the urllib error.
"""

from __future__ import annotations

import http.client
import re
import ssl
import urllib.error
import urllib.request
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Final
from urllib.parse import parse_qsl, urljoin, urlsplit

from rheo_finance.context import UnitOfWork
from rheo_finance.render import redact
from rheo_finance.sinks.gate import (
    AllowlistRefused,
    HostRefused,
    SinkDisabled,
    enabled_digest,
)
from rheo_finance.sinks.spec import RequestTemplate, SinkSpec, spec_digest

__all__ = [
    "AllowlistRefused",
    "Client",
    "HostRefused",
    "RequestFailed",
    "Response",
    "SinkDisabled",
]

SCHEMES: Final = frozenset({"https", "http"})
DEFAULT_PORTS: Final = {"https": 443, "http": 80}
# Plain http is for a test against a stub on this machine, never anywhere else.
LOOPBACK_HOSTS: Final = frozenset({"127.0.0.1", "localhost"})
# Shorter values are not scrubbed: they would mangle the message and hide nothing.
_SCRUB_MIN: Final = 4
_ERROR_MAX: Final = 200
_PARAM_SEGMENT: Final = re.compile(r"[A-Za-z0-9._~-]+")


class RequestFailed(Exception):
    """The request reached for the network and failed (refused, reset, timed out)."""


@dataclass(frozen=True)
class Response:
    status: int
    headers: tuple[tuple[str, str], ...]
    body: bytes

    def header(self, name: str) -> str | None:
        wanted = name.lower()
        return next((v for k, v in self.headers if k.lower() == wanted), None)


@dataclass(frozen=True)
class _Origin:
    scheme: str
    host: str
    port: int

    def entry(self) -> str:
        """The ``SinkSpec.hosts`` entry that names this origin."""
        if self.port == DEFAULT_PORTS[self.scheme]:
            return self.host
        return f"{self.host}:{self.port}"


def _origin(url: str, sink: str) -> _Origin:
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        raise HostRefused(f"{sink}: the URL does not parse") from None
    if parts.username is not None or parts.password is not None:
        raise HostRefused(f"{sink}: a URL with credentials in it is refused")
    host = parts.hostname
    if not host:
        raise HostRefused(f"{sink}: the URL names no host")
    scheme = parts.scheme.lower()
    if scheme not in SCHEMES:
        raise HostRefused(f"{sink}: scheme {scheme or 'none'} is refused")
    return _Origin(scheme, host, port if port is not None else DEFAULT_PORTS[scheme])


def _path_matches(template: str, path: str) -> bool:
    want = template.split("/")
    have = path.split("/")
    if len(want) != len(have):
        return False
    for expected, segment in zip(want, have, strict=True):
        if expected.startswith(":"):
            if segment in {".", ".."} or not _PARAM_SEGMENT.fullmatch(segment):
                return False
        elif expected != segment:
            return False
    return True


class Client:
    """An HTTP client for one sink, limited to that sink's hosts and an allowlist.

    ``allowlist`` is the (method, path) pairs this client may use; each must be one of
    the spec's request templates. ``allowed_schemes`` holds ``http`` only in tests
    against a stub on 127.0.0.1 or localhost.
    """

    def __init__(
        self,
        sink: str,
        spec: SinkSpec,
        allowlist: tuple[tuple[str, str], ...],
        *,
        uow: UnitOfWork,
        timeout: float = 10.0,
        allowed_schemes: tuple[str, ...] = ("https",),
    ) -> None:
        if spec.name != sink:
            raise ValueError(f"{sink}: the spec is for another sink")
        if not allowed_schemes or not set(allowed_schemes) <= SCHEMES:
            raise ValueError("allowed_schemes is https, or http for a local stub")
        if timeout <= 0:
            raise ValueError("timeout is a positive number of seconds")
        declared = {(each.method, each.path): each for each in spec.requests}
        allowed: dict[tuple[str, str], RequestTemplate] = {}
        for method, path in allowlist:
            template = declared.get((method, path))
            if template is None:
                raise AllowlistRefused(
                    f"{sink}: {method} {path} is not a declared request"
                )
            allowed[(method, path)] = template
        self.sink = sink
        self.spec = spec
        self.timeout = timeout
        self.allowed_schemes = frozenset(allowed_schemes)
        self._allowed = allowed
        self._opener = urllib.request.OpenerDirector()
        self._opener.add_handler(
            urllib.request.HTTPSHandler(context=ssl.create_default_context())
        )
        if "http" in self.allowed_schemes:
            self._opener.add_handler(urllib.request.HTTPHandler())
        self._uow = uow

    def __repr__(self) -> str:
        return f"Client(sink={self.sink!r})"

    # --- the checks, in order ---------------------------------------------------------

    def _check_gate(self) -> None:
        approved = enabled_digest(self._uow, self.sink)
        if approved is None or approved != spec_digest(self.spec):
            raise SinkDisabled(
                f"{self.sink}: the sink is disabled (setting off, no current approval,"
                " or the approval is stale)"
            )

    def _check_host(self, origin: _Origin) -> None:
        if origin.scheme not in self.allowed_schemes:
            raise HostRefused(f"{self.sink}: scheme {origin.scheme} is refused")
        if origin.scheme == "http" and origin.host not in LOOPBACK_HOSTS:
            raise HostRefused(f"{self.sink}: plain http is for a local stub only")
        if origin.entry() not in self.spec.hosts:
            raise HostRefused(f"{self.sink}: host {origin.entry()} is not declared")

    def _check_allowlist(self, method: str, url: str, secrets: list[str]) -> None:
        parts = urlsplit(url)
        path = parts.path or "/"
        shown = _scrub(path, secrets)
        if parts.fragment:
            raise AllowlistRefused(f"{self.sink}: a URL with a fragment is refused")
        template = next(
            (
                each
                for (allowed_method, _), each in self._allowed.items()
                if allowed_method == method and _path_matches(each.path, path)
            ),
            None,
        )
        if template is None:
            raise AllowlistRefused(
                f"{self.sink}: {method} {shown} is not on the allowlist"
            )
        keys = {key for key, _ in parse_qsl(parts.query, keep_blank_values=True)}
        if parts.query and not keys:
            raise AllowlistRefused(
                f"{self.sink}: {method} {shown}: the query is malformed"
            )
        extra = sorted(keys - set(template.query_keys))
        if extra:
            named = ", ".join(_scrub(key, secrets) for key in extra)
            raise AllowlistRefused(
                f"{self.sink}: {method} {shown} does not take query key {named}"
            )

    # --- the request ------------------------------------------------------------------

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        body: bytes | None = None,
    ) -> Response:
        headers = dict(headers or {})
        secrets = _secrets(headers.values(), url)
        self._check_gate()
        origin = _origin(url, self.sink)
        self._check_host(origin)
        self._check_allowlist(method, url, secrets)

        path = _scrub(urlsplit(url).path, secrets)
        where = f"{self.sink}: {method} {origin.entry()}{path}"
        outgoing = urllib.request.Request(
            url, data=body, headers=headers, method=method
        )
        try:
            with self._opener.open(outgoing, timeout=self.timeout) as raw:
                status = int(raw.status)
                received = tuple((k, v) for k, v in raw.headers.items())
                content = raw.read()
        except (OSError, http.client.HTTPException, urllib.error.URLError) as exc:
            detail = _scrub(str(getattr(exc, "reason", exc)), secrets)[:_ERROR_MAX]
            raise RequestFailed(
                f"{where} failed: {type(exc).__name__}: {detail}".rstrip(": ")
            ) from None
        response = Response(status=status, headers=received, body=content)
        if 300 <= status < 400:
            location = response.header("Location")
            if location is not None:
                target = _origin(urljoin(url, location), self.sink)
                if target != origin:
                    raise HostRefused(
                        f"{self.sink}: a redirect to another host is refused"
                    )
        return response


def _secrets(header_values: Iterable[str], url: str) -> list[str]:
    """Every header value and query value long enough to be worth scrubbing."""
    try:
        query = parse_qsl(urlsplit(url).query, keep_blank_values=True)
    except ValueError:
        query = []
    found: list[str] = []
    for value in [*header_values, *(value for _, value in query)]:
        found += [value, *value.split()]  # "Bearer <key>" scrubs the key alone too
    # Longest first, so a value that contains another is scrubbed whole.
    return sorted(
        {value for value in found if len(value) >= _SCRUB_MIN}, key=len, reverse=True
    )


def _scrub(text: str, secrets: list[str]) -> str:
    for secret in secrets:
        text = text.replace(secret, "<redacted>")
    return redact(text)
