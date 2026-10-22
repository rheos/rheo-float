"""``rheo_finance.http.Client`` against a local stub server (FR-65, AC-78 (b), (f)).

Every refusal is checked at the stub: a disabled, stale, off-host, off-allowlist or
redirected request records zero requests (one, for the redirect) and nothing reaches
the other host. The stub speaks plain http on 127.0.0.1, so each client here passes
``allowed_schemes=("http",)``. The test-only ``rates`` provider declares the stub's
host. Keys and values are synthetic.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Callable
from dataclasses import replace
from typing import TYPE_CHECKING, Any

import pytest
from sqlalchemy import Connection

from rheo_finance import operations
from rheo_finance.clock import FakeClock
from rheo_finance.context import FinanceContext, UnitOfWork
from rheo_finance.contracts import Role
from rheo_finance.http import (
    AllowlistRefused,
    Client,
    HostRefused,
    RequestFailed,
    SinkDisabled,
)
from rheo_finance.sinks.spec import SINK_SPECS, RequestTemplate, SinkSpec, spec_digest

if TYPE_CHECKING:
    from conftest import StubServer

SINK = "rates"
T0 = dt.datetime(2026, 1, 15, 9, 0, tzinfo=dt.UTC)
HOUR = dt.timedelta(hours=1)
KEY = "999-synthetic-key-0f3a9c"
SERIES = ("GET", "/series/:code")
ALLOWLIST = (SERIES,)


class StubSink:
    """The ``rates`` provider for these tests: the stub's host and two requests."""

    __test__ = False

    def __init__(self, host: str) -> None:
        self.hosts: tuple[str, ...] = (host,)
        self.requests: tuple[RequestTemplate, ...] = (
            RequestTemplate(
                method="GET", path="/series/:code", query_keys=("start", "api_key")
            ),
            RequestTemplate(method="POST", path="/notes"),
        )
        self.data: tuple[tuple[str, str], ...] = (("currencies", "USD"),)

    def __call__(self, _uow: UnitOfWork) -> SinkSpec:
        return SinkSpec(
            name=SINK,
            hosts=self.hosts,
            requests=self.requests,
            key_file="rates.key",
            payload_policy_version=1,
            data_inputs=self.data,
        )


@pytest.fixture
def uow(owner_conn: Connection) -> UnitOfWork:
    return UnitOfWork(owner_conn)


@pytest.fixture
def provider(stub_server: StubServer, monkeypatch: pytest.MonkeyPatch) -> StubSink:
    sink = StubSink(stub_server.host)
    monkeypatch.setitem(SINK_SPECS, SINK, sink)
    return sink


@pytest.fixture
def enabled(
    uow: UnitOfWork,
    provider: StubSink,
    sink_settings: Callable[..., None],
    record_sink_approval: Callable[..., uuid.UUID],
) -> None:
    """The sink set true and approved for the provider's current spec."""
    sink_settings(rates=True)
    record_sink_approval(SINK, spec_digest(provider(uow)), at=T0)


def client(uow: UnitOfWork, provider: StubSink, **options: Any) -> Client:
    options.setdefault("allowed_schemes", ("http",))
    allowlist = options.pop("allowlist", ALLOWLIST)
    return Client(SINK, provider(uow), allowlist, uow=uow, **options)


# --- the enabled case -----------------------------------------------------------------


def test_an_enabled_sink_sends_exactly_once(
    enabled: None, uow: UnitOfWork, provider: StubSink, stub_server: StubServer
) -> None:
    stub_server.reply("/series/FXUSDCAD", body=b"rates")
    response = client(uow, provider).request(
        "GET", stub_server.url("/series/FXUSDCAD?start=2026-01-01")
    )
    assert response.status == 200
    assert response.body == b"rates"
    assert [(r.method, r.path) for r in stub_server.requests] == [
        ("GET", "/series/FXUSDCAD?start=2026-01-01")
    ]


def test_every_declared_key_value_part_is_accepted(
    enabled: None, uow: UnitOfWork, provider: StubSink, stub_server: StubServer
) -> None:
    path = "/series/FXUSDCAD?start=2026-01-01&api_key=&start=2026-02-01"
    client(uow, provider).request("GET", stub_server.url(path))
    assert [r.path for r in stub_server.requests] == [path]


# --- the gate, before any socket (AC-78b, AC-78f) -------------------------------------


def test_a_setting_set_by_hand_with_no_approval_sends_nothing(
    uow: UnitOfWork,
    provider: StubSink,
    stub_server: StubServer,
    sink_settings: Callable[..., None],
) -> None:
    sink_settings(rates=True)
    with pytest.raises(SinkDisabled):
        client(uow, provider).request("GET", stub_server.url("/series/FXUSDCAD"))
    assert stub_server.requests == []


def test_an_approval_without_the_setting_sends_nothing(
    uow: UnitOfWork,
    provider: StubSink,
    stub_server: StubServer,
    sink_settings: Callable[..., None],
    record_sink_approval: Callable[..., uuid.UUID],
) -> None:
    sink_settings(rates=False)
    record_sink_approval(SINK, spec_digest(provider(uow)), at=T0)
    with pytest.raises(SinkDisabled):
        client(uow, provider).request("GET", stub_server.url("/series/FXUSDCAD"))
    assert stub_server.requests == []


def add_host(provider: StubSink) -> None:
    provider.hosts = (*provider.hosts, "more.example.test")


def add_request(provider: StubSink) -> None:
    provider.requests = (*provider.requests, RequestTemplate(method="GET", path="/x"))


def add_data_input(provider: StubSink) -> None:
    provider.data = (("currencies", "EUR,USD"),)


@pytest.mark.parametrize("widen", [add_host, add_request, add_data_input])
def test_a_widened_spec_makes_the_approval_stale_and_sends_nothing(
    widen: Callable[[StubSink], None],
    enabled: None,
    uow: UnitOfWork,
    provider: StubSink,
    stub_server: StubServer,
) -> None:
    widen(provider)
    with pytest.raises(SinkDisabled):
        client(uow, provider).request("GET", stub_server.url("/series/FXUSDCAD"))
    assert stub_server.requests == []


def test_a_client_built_on_a_wider_spec_than_the_approved_one_sends_nothing(
    enabled: None, uow: UnitOfWork, provider: StubSink, stub_server: StubServer
) -> None:
    wider = provider(uow).model_copy(
        update={"hosts": (stub_server.host, "more.example.test")}
    )
    with pytest.raises(SinkDisabled):
        Client(SINK, wider, ALLOWLIST, uow=uow, allowed_schemes=("http",)).request(
            "GET", stub_server.url("/series/FXUSDCAD")
        )
    assert stub_server.requests == []


@pytest.fixture
def owner(rolled_back_context: Callable[[Role], FinanceContext]) -> FinanceContext:
    return replace(rolled_back_context(Role.OWNER), clock=FakeClock(T0 + HOUR))


def test_a_reversed_newest_approval_sends_nothing_despite_an_older_one(
    uow: UnitOfWork,
    owner: FinanceContext,
    provider: StubSink,
    stub_server: StubServer,
    sink_settings: Callable[..., None],
    record_sink_approval: Callable[..., uuid.UUID],
) -> None:
    sink_settings(rates=True)
    digest = spec_digest(provider(uow))
    record_sink_approval(SINK, digest, at=T0 - HOUR)
    newer = record_sink_approval(SINK, digest, at=T0)
    operations.dispatch(
        "finance.decision.reverse", {"decision_id": newer}, Role.OWNER, ctx=owner
    )
    with pytest.raises(SinkDisabled):
        client(uow, provider).request("GET", stub_server.url("/series/FXUSDCAD"))
    assert stub_server.requests == []
    record_sink_approval(SINK, digest, at=T0 + 2 * HOUR)
    client(uow, provider).request("GET", stub_server.url("/series/FXUSDCAD"))
    assert len(stub_server.requests) == 1


# --- hosts and schemes ----------------------------------------------------------------


def test_a_host_outside_the_spec_raises_before_the_socket(
    enabled: None,
    uow: UnitOfWork,
    provider: StubSink,
    stub_server: StubServer,
    make_stub_server: Callable[[], StubServer],
) -> None:
    other = make_stub_server()
    with pytest.raises(HostRefused):
        client(uow, provider).request("GET", other.url("/series/FXUSDCAD"))
    assert stub_server.requests == [] and other.requests == []


def test_https_is_the_only_scheme_by_default(
    enabled: None, uow: UnitOfWork, provider: StubSink, stub_server: StubServer
) -> None:
    with pytest.raises(HostRefused, match="scheme http"):
        client(uow, provider, allowed_schemes=("https",)).request(
            "GET", stub_server.url("/series/FXUSDCAD")
        )
    assert stub_server.requests == []


@pytest.mark.parametrize(
    "url",
    [
        "http://rates.example.test/series/FXUSDCAD",  # plain http off this machine
        "ftp://rates.example.test/series/FXUSDCAD",
        "http://user:pass@127.0.0.1/series/FXUSDCAD",
    ],
)
def test_other_schemes_remote_http_and_credentials_are_refused(
    url: str,
    uow: UnitOfWork,
    provider: StubSink,
    sink_settings: Callable[..., None],
    record_sink_approval: Callable[..., uuid.UUID],
) -> None:
    provider.hosts = (*provider.hosts, "rates.example.test", "127.0.0.1")
    sink_settings(rates=True)
    record_sink_approval(SINK, spec_digest(provider(uow)), at=T0)
    with pytest.raises(HostRefused):
        client(uow, provider).request("GET", url)


def test_a_redirect_to_another_host_is_refused_not_followed(
    enabled: None,
    uow: UnitOfWork,
    provider: StubSink,
    stub_server: StubServer,
    make_stub_server: Callable[[], StubServer],
) -> None:
    other = make_stub_server()
    stub_server.redirect("/series/FXUSDCAD", other.url("/landing"))
    with pytest.raises(HostRefused, match="redirect"):
        client(uow, provider).request("GET", stub_server.url("/series/FXUSDCAD"))
    assert len(stub_server.requests) == 1
    assert other.requests == []


def test_a_redirect_on_the_same_host_is_returned_not_followed(
    enabled: None, uow: UnitOfWork, provider: StubSink, stub_server: StubServer
) -> None:
    stub_server.redirect("/series/FXUSDCAD", "/series/FXEURCAD")
    response = client(uow, provider).request("GET", stub_server.url("/series/FXUSDCAD"))
    assert response.status == 302
    assert response.header("location") == "/series/FXEURCAD"
    assert [r.path for r in stub_server.requests] == ["/series/FXUSDCAD"]


# --- the allowlist --------------------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/other"),
        ("POST", "/series/FXUSDCAD"),  # declared path, method not allowed
        ("POST", "/notes"),  # declared in the spec, not on this client's allowlist
        ("GET", "/series/FXUSDCAD/extra"),
        ("GET", "/series/.."),
        ("GET", "/series/FXUSDCAD?limit=5"),  # query key not declared
        ("GET", "/series/FXUSDCAD#part"),
        ("GET", "/series/FXUSDCAD?start=1;api_key=x"),  # a ; is refused
        ("GET", "/series/FXUSDCAD?start"),  # a part with no =
        ("GET", "/series/FXUSDCAD?start=1&"),  # an empty part
        ("GET", "/series/FXUSDCAD?&start=1"),
        ("GET", "/series/FXUSDCAD?=1"),  # no key
        ("GET", "/series/FXUSDCAD?st%61rt=1"),  # keys compare as written
    ],
)
def test_a_request_outside_the_allowlist_raises_before_the_socket(
    method: str,
    path: str,
    enabled: None,
    uow: UnitOfWork,
    provider: StubSink,
    stub_server: StubServer,
) -> None:
    with pytest.raises(AllowlistRefused):
        client(uow, provider).request(method, stub_server.url(path))
    assert stub_server.requests == []


def test_an_allowlist_entry_the_spec_does_not_declare_is_refused(
    uow: UnitOfWork, provider: StubSink
) -> None:
    with pytest.raises(AllowlistRefused):
        client(uow, provider, allowlist=(("DELETE", "/series/:code"),))


# --- keys never surface ---------------------------------------------------------------


def assert_no_key(exc: BaseException) -> None:
    seen: BaseException | None = exc
    while seen is not None:
        assert KEY not in str(seen) and KEY not in repr(seen)
        seen = seen.__cause__ or seen.__context__


def assert_unchained(exc: BaseException) -> None:
    """The urllib error, whose text the client does not control, is not attached."""
    assert exc.__cause__ is None and exc.__context__ is None


def test_a_key_never_appears_in_a_transport_error(
    enabled: None, uow: UnitOfWork, provider: StubSink, stub_server: StubServer
) -> None:
    url = stub_server.url(f"/series/FXUSDCAD?api_key={KEY}")
    stub_server.stop()  # nothing listens on the port now
    with pytest.raises(RequestFailed) as caught:
        client(uow, provider).request(
            "GET", url, headers={"Authorization": f"Bearer {KEY}"}
        )
    assert_no_key(caught.value)
    assert_unchained(caught.value)
    assert "rates: GET" in str(caught.value)


def test_a_timeout_is_a_request_failure_without_the_key(
    enabled: None, uow: UnitOfWork, provider: StubSink, stub_server: StubServer
) -> None:
    stub_server.reply("/series/FXUSDCAD", delay=1.0)
    with pytest.raises(RequestFailed) as caught:
        client(uow, provider, timeout=0.2).request(
            "GET",
            stub_server.url(f"/series/FXUSDCAD?api_key={KEY}"),
            headers={"X-Api-Key": KEY},
        )
    assert_no_key(caught.value)
    assert_unchained(caught.value)


@pytest.mark.parametrize(
    ("path", "error"),
    [
        (f"/other?api_key={KEY}", AllowlistRefused),
        (f"/series/FXUSDCAD?{KEY}=1", AllowlistRefused),
        (f"/series/{KEY}?bad=1", AllowlistRefused),
    ],
)
def test_a_key_never_appears_in_a_refusal(
    path: str,
    error: type[Exception],
    enabled: None,
    uow: UnitOfWork,
    provider: StubSink,
    stub_server: StubServer,
) -> None:
    with pytest.raises(error) as caught:
        client(uow, provider).request(
            "GET", stub_server.url(path), headers={"Authorization": f"Bearer {KEY}"}
        )
    assert_no_key(caught.value)
    assert stub_server.requests == []


def test_the_key_reaches_the_stub_as_sent(
    enabled: None, uow: UnitOfWork, provider: StubSink, stub_server: StubServer
) -> None:
    client(uow, provider).request(
        "GET", stub_server.url("/series/FXUSDCAD"), headers={"X-Api-Key": KEY}
    )
    (seen,) = stub_server.requests
    assert seen.headers["X-Api-Key"] == KEY


@pytest.mark.parametrize(
    "headers",
    [
        {"Authorization": f"Bearer {KEY}\n"},  # a value http.client refuses
        {f"X-{KEY}\n": "1"},  # a name it refuses
    ],
)
def test_an_invalid_header_is_a_request_failure_without_the_key(
    headers: dict[str, str],
    enabled: None,
    uow: UnitOfWork,
    provider: StubSink,
    stub_server: StubServer,
) -> None:
    with pytest.raises(RequestFailed) as caught:
        client(uow, provider).request(
            "GET", stub_server.url("/series/FXUSDCAD"), headers=headers
        )
    assert_no_key(caught.value)
    assert_unchained(caught.value)
    assert stub_server.requests == []
