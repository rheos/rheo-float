"""Which modules register a sink provider or preview builder, seen at import.

``test_sink_providers`` runs this file as a script in a fresh interpreter (a module
already imported adds nothing twice). Before anything else of the package loads, it
swaps ``sinks.spec.SINK_SPECS`` and ``sinks.preview.PREVIEW_BUILDERS`` for recording
dicts; then it imports every module under the ``rheo_finance`` package it finds on
``sys.path`` but the prefixes given as arguments, and writes one JSON object to stdout:

- ``package``: the directory of the package it scanned;
- ``added``: for each module whose import added a key, ``"<registry>:<key>"`` per key,
  by whatever route (``R[k] = v``, ``R.update(...)``, ``R.setdefault(k, v)``,
  ``R |= {...}``, ``R.__setitem__(k, v)``, through an alias or an attribute). The
  module is the innermost module body on the stack when the key was added, so a module
  that merely imports a registering one is not blamed, while one that registers through
  a helper function another module defines is;
- ``unattributed``: keys that reached the original dicts instead (a module imported
  before the swap had bound them), which the test refuses.
"""

from __future__ import annotations

import importlib
import json
import sys
from collections.abc import Iterable
from pathlib import Path
from types import FrameType
from typing import Any, Self

from rheo_finance.sinks import preview, spec

Added = dict[str, list[str]]


def _importing_module() -> str:
    """The ``__name__`` of the innermost module body on the stack, else of the frame
    that called into the registry."""
    caller = sys._getframe(3)
    frame: FrameType | None = caller
    while frame is not None:
        if frame.f_code.co_name == "<module>":
            return str(frame.f_globals.get("__name__", "?"))
        frame = frame.f_back
    return str(caller.f_globals.get("__name__", "?"))


class Recording(dict[str, Any]):
    """A dict that notes which module adds each new key."""

    def __init__(self, registry: str, added: Added) -> None:
        super().__init__()
        self._registry = registry
        self._added = added

    def _note(self, keys: Iterable[str]) -> None:
        module = _importing_module()
        for key in keys:
            if key not in self:
                self._added.setdefault(module, []).append(f"{self._registry}:{key}")

    def __setitem__(self, key: str, value: Any) -> None:
        self._note([key])
        super().__setitem__(key, value)

    def setdefault(self, key: str, default: Any = None) -> Any:
        self._note([key])
        return super().setdefault(key, default)

    def update(self, *args: Any, **kwargs: Any) -> None:
        other = dict(*args, **kwargs)
        self._note(other)
        super().update(other)

    # dict's ``__or__`` widens the key and value types, which an in-place ``|=`` on a
    # recording dict never does; mypy compares the two signatures all the same.
    def __ior__(self, other: Any) -> Self:  # type: ignore[misc,override]
        found = dict(other)
        self._note(found)
        super().update(found)
        return self


def module_names(package: Path, skip: tuple[str, ...]) -> list[str]:
    """Every module under ``package`` (``src/rheo_finance`` or a copy of it), named
    as ``source_tree.modules`` names it, less the ``skip`` prefixes."""
    names: list[str] = []
    for path in sorted(package.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        relative = path.relative_to(package.parent).with_suffix("")
        name = ".".join(relative.parts).removesuffix(".__init__")
        if any(name == each or name.startswith(each + ".") for each in skip):
            continue
        names.append(name)
    return names


def main(argv: list[str]) -> int:
    added: Added = {}
    original_specs, original_builders = spec.SINK_SPECS, preview.PREVIEW_BUILDERS
    spec.SINK_SPECS = Recording("SINK_SPECS", added)
    preview.PREVIEW_BUILDERS = Recording("PREVIEW_BUILDERS", added)
    package = Path(importlib.import_module("rheo_finance").__file__ or "").parent
    for name in module_names(package, tuple(argv)):
        importlib.import_module(name)
    unattributed = [f"SINK_SPECS:{key}" for key in sorted(original_specs)] + [
        f"PREVIEW_BUILDERS:{key}" for key in sorted(original_builders)
    ]
    report = {
        "package": str(package),
        "added": {name: sorted(keys) for name, keys in sorted(added.items())},
        "unattributed": unattributed,
    }
    sys.stdout.write(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
