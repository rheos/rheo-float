"""Every module that registers operations, so the whole catalogue can be loaded.

A module that calls ``operations.register`` at import adds its dotted name to
``MODULES`` in the same change. ``load_all`` imports them all, and the invariant tests
call it, so an operation that no test imports is still checked.
"""

from __future__ import annotations

import importlib

from rheo_finance import operations

MODULES: tuple[str, ...] = (
    "rheo_finance.decision",
    "rheo_finance.sinks.ops",
    "rheo_finance.documents.registry",
    "rheo_finance.derive.ops",
    "rheo_finance.reports.ops",
    "rheo_finance.rates.valet",
)


def load_all() -> dict[str, operations.OperationSpec]:
    """Import every module in ``MODULES`` and return the registry."""
    for module in MODULES:
        importlib.import_module(module)
    return operations.SPECS
