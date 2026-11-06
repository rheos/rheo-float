"""Helpers of the rates tests: Valet's reply shape and a client that allows http. The
``valet_stub`` fixture that uses them is in ``tests/conftest.py``, with every other
fixture, and imports from here.
"""

from __future__ import annotations

import json
from collections.abc import Sequence

from rheo_finance.context import UnitOfWork
from rheo_finance.http import Client
from rheo_finance.rates import valet
from rheo_finance.sinks.spec import SinkSpec


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
