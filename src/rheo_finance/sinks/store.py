"""The derived ``sink_spec`` table (derivation stage 16).

``write_sink_specs`` replaces every row with one per registered provider: the spec's
digest (the same ``spec_digest`` the gate computes) and its parts as public text, so a
reader-side view can tell a stale approval without running code. Derive calls it as its
last step, and ``finance.sink.approve`` calls it too, so ``report_sink`` is current
right after an approval. ``finance.sink.disable`` rewrites only its sink's row
(``write_sink_spec``), after the disable is recorded and in a savepoint, so a failing
provider can never undo a disable. ``computed_at`` is the database's
transaction time and is left out of the ledger digest.

``PROVIDER_MODULES`` lists every module that registers a provider (and its preview
builder) at import. This module imports them all, so derive and the sink operations,
which import it, always see every sink's spec. A module that registers one adds its
name here in the same change.
"""

from __future__ import annotations

import importlib
from typing import Final

from sqlalchemy import delete, insert

from rheo_finance.context import UnitOfWork
from rheo_finance.sinks.spec import SINK_SPECS, SinkSpec, current_spec, spec_digest
from rheo_finance.storage.schema import sink_spec

PROVIDER_MODULES: Final[tuple[str, ...]] = ("rheo_finance.rates.valet",)
for _module in PROVIDER_MODULES:
    importlib.import_module(_module)


def spec_row(spec: SinkSpec) -> dict[str, object]:
    """The ``sink_spec`` columns for ``spec``, ``computed_at`` aside."""
    return {
        "sink": spec.name,
        "spec_digest": spec_digest(spec),
        "hosts": "\n".join(spec.hosts),
        "request_templates": "\n".join(each.as_line() for each in spec.requests),
        "key_file": spec.key_file,
        "policy_version": spec.payload_policy_version,
        "data_inputs": "\n".join(f"{name}={value}" for name, value in spec.data_inputs),
    }


def write_sink_spec(uow: UnitOfWork, sink: str) -> None:
    """Rewrite ``sink``'s row alone: its provider's spec, or no row without one."""
    spec = current_spec(uow, sink)
    uow.execute(delete(sink_spec).where(sink_spec.c.sink == sink))
    if spec is not None:
        uow.execute(insert(sink_spec).values(**spec_row(spec)))


def write_sink_specs(uow: UnitOfWork) -> None:
    """Rewrite ``sink_spec`` from every registered provider."""
    rows = []
    for sink in sorted(SINK_SPECS):
        spec = current_spec(uow, sink)
        assert spec is not None  # sink is a key of SINK_SPECS
        rows.append(spec_row(spec))
    uow.execute(delete(sink_spec))
    if rows:
        uow.execute(insert(sink_spec), rows)
