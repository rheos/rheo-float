"""Fixtures for the rates tests: the derive ``ledger`` and ``fx_ledger``, and Valet
pointed at a local stub server.

``valet_stub`` swaps ``rates.valet.ENDPOINT`` for the stub's host over plain http and
``make_client`` for one that allows http, for one test; the provider then declares the
stub's host, so an approval in the test is pinned to that spec. Nothing reaches the
real Valet.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import TYPE_CHECKING

import pytest

from derive.conftest import fx_ledger, ledger  # noqa: F401 (shared fixtures)
from rheo_finance.context import UnitOfWork
from rheo_finance.http import Client
from rheo_finance.rates import valet
from rheo_finance.sinks.spec import SinkSpec

if TYPE_CHECKING:
    from conftest import StubServer


def valet_reply(series: str, rows: Sequence[tuple[str, str | None]]) -> bytes:
    """A reply shaped like Valet's observations JSON: ``(date, value)`` rows, a None
    value standing for a date the series has no value on."""
    observations = [
        {"d": day} if value is None else {"d": day, series: {"v": value}}
        for day, value in rows
    ]
    return json.dumps(
        {
            "terms": {"url": "https://example.test/terms"},
            "seriesDetail": {series: {"label": f"{series[2:5]}/CAD"}},
            "observations": observations,
        }
    ).encode("utf-8")


def stub_client(uow: UnitOfWork, spec: SinkSpec) -> Client:
    return Client(valet.SINK, spec, valet.ALLOWLIST, uow=uow, allowed_schemes=("http",))


@pytest.fixture
def valet_stub(stub_server: StubServer, monkeypatch: pytest.MonkeyPatch) -> StubServer:
    monkeypatch.setattr(valet, "ENDPOINT", valet.Endpoint(stub_server.host, "http"))
    monkeypatch.setattr(valet, "make_client", stub_client)
    return stub_server
