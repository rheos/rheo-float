"""Every module that registers a sink provider or preview builder is one
``sinks.store`` imports (``PROVIDER_MODULES``), so derive, the gate's callers and the
sink operations never miss a sink's spec.

The check runs the registrations, it does not read for them: ``provider_scan.py`` runs
in a fresh interpreter (an import that already happened adds nothing twice), swaps the
two registries for recording dicts before anything else of the package loads, imports
every module under ``src/rheo_finance`` but ``source_tree.NOT_IMPORTED`` (Alembic's
env runs its migration at import) and reports which module's import added a key, by
whatever route: a subscript, ``update``, ``setdefault``, ``|=``, ``__setitem__``,
through an alias or an attribute. The control plants one module per route in a copy of
the package and scans that copy instead.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from rheo_finance.sinks.store import PROVIDER_MODULES

from .source_tree import NOT_IMPORTED, PACKAGE, copy_package

SCAN = Path(__file__).with_name("provider_scan.py")


def registering_modules(package: Path = PACKAGE) -> dict[str, list[str]]:
    """``{module: ["<registry>:<key>", ...]}`` for every module whose import adds a
    key, observed in a fresh interpreter importing ``package``."""
    env = dict(os.environ)
    if package != PACKAGE:
        # The copy shadows the installed package: PYTHONPATH precedes site-packages.
        env["PYTHONPATH"] = str(package.parent)
    done = subprocess.run(
        [sys.executable, str(SCAN), *NOT_IMPORTED],
        capture_output=True,
        text=True,
        env=env,
        check=False,
        timeout=300,
    )
    assert done.returncode == 0, done.stderr[-2000:]
    report = json.loads(done.stdout)
    assert Path(report["package"]) == package, "the scan imported another tree"
    assert report["unattributed"] == [], report["unattributed"]
    found: dict[str, list[str]] = report["added"]
    return found


def test_every_registering_module_is_a_provider_module() -> None:
    found = registering_modules()
    assert set(found) == set(PROVIDER_MODULES)
    assert found["rheo_finance.rates.valet"] == [
        "PREVIEW_BUILDERS:rates",
        "SINK_SPECS:rates",
    ]


def test_a_planted_registration_is_found_by_every_route(tmp_path: Path) -> None:
    root = copy_package(tmp_path)
    planted = {
        "ior": (
            "from rheo_finance.sinks.spec import SINK_SPECS\n"
            "SINK_SPECS |= {'coinstats': lambda uow: None}\n"
        ),
        "alias": (
            "from rheo_finance.sinks.spec import SINK_SPECS as S\n"
            "S['kraken'] = lambda uow: None\n"
        ),
        "setitem": (
            "from rheo_finance.sinks import preview\n"
            "preview.PREVIEW_BUILDERS.__setitem__('kraken', None)\n"
        ),
        "update": (
            "from rheo_finance.sinks.preview import PREVIEW_BUILDERS\n"
            "PREVIEW_BUILDERS.update(coinstats=None)\n"
        ),
        "setdefault": (
            "from rheo_finance.sinks import spec\n"
            "spec.SINK_SPECS.setdefault('telegram', lambda uow: None)\n"
        ),
        "helper": (
            "from rheo_finance.sinks.spec import SINK_SPECS\n"
            "def register(name, provider):\n"
            "    SINK_SPECS[name] = provider\n"
        ),
        "via_helper": (
            "from rheo_finance.rates.planted_helper import register\n"
            "register('mot', lambda uow: None)\n"
        ),
    }
    for name, source in planted.items():
        (root / "rates" / f"planted_{name}.py").write_text(source, encoding="utf-8")

    found = registering_modules(root)

    assert {k: v for k, v in found.items() if k not in PROVIDER_MODULES} == {
        "rheo_finance.rates.planted_alias": ["SINK_SPECS:kraken"],
        "rheo_finance.rates.planted_ior": ["SINK_SPECS:coinstats"],
        "rheo_finance.rates.planted_setdefault": ["SINK_SPECS:telegram"],
        "rheo_finance.rates.planted_setitem": ["PREVIEW_BUILDERS:kraken"],
        "rheo_finance.rates.planted_update": ["PREVIEW_BUILDERS:coinstats"],
        # The module whose import registered, not the one that defined the helper.
        "rheo_finance.rates.planted_via_helper": ["SINK_SPECS:mot"],
    }
    assert set(found) & set(PROVIDER_MODULES) == set(PROVIDER_MODULES)
