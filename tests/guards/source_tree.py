"""What the guard tests read: the source tree, its imports, and the modules it loads.

Every guard enumerates what exists by walking ``src/rheo_finance`` (and ``scripts/``
where it says so), never a list of tables or modules. Each scan takes the root it walks,
so a test can run it over a temporary copy of the tree with a violation planted in it.
"""

from __future__ import annotations

import ast
import importlib
import re
import shutil
import tomllib
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any

from pydantic import BaseModel

import rheo_finance

PACKAGE = Path(rheo_finance.__file__).resolve().parent
REPO = PACKAGE.parent.parent
SCRIPTS = REPO / "scripts"
PYPROJECT = REPO / "pyproject.toml"
UV_LOCK = REPO / "uv.lock"
# Alembic's env runs its migration at import, and revisions are frozen text: neither is
# imported to collect registrations.
NOT_IMPORTED = ("rheo_finance.migrations",)


@dataclass(frozen=True)
class Module:
    name: str
    path: Path
    tree: ast.Module

    @property
    def is_package(self) -> bool:
        return self.path.name == "__init__.py"


def modules(root: Path = PACKAGE) -> Iterator[Module]:
    """Every ``.py`` file under ``root``, named as a module under ``root``'s own name
    (``root`` is a package directory such as ``src/rheo_finance``, or ``scripts``)."""
    for path in sorted(root.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        relative = path.relative_to(root.parent).with_suffix("")
        name = ".".join(relative.parts).removesuffix(".__init__")
        yield Module(name, path, ast.parse(path.read_text(encoding="utf-8")))


def copy_package(tmp_path: Path) -> Path:
    """A copy of ``src/rheo_finance`` under ``tmp_path``, for planting a violation."""
    target = tmp_path / PACKAGE.name
    shutil.copytree(PACKAGE, target, ignore=shutil.ignore_patterns("__pycache__"))
    return target


def plant(root: Path, relative: str, source: str) -> Path:
    """Write ``source`` to ``root/relative``, making each directory on the way a
    package."""
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    directory = path.parent
    while directory != root:
        (directory / "__init__.py").touch(exist_ok=True)
        directory = directory.parent
    path.write_text(source, encoding="utf-8")
    return path


def _from_base(node: ast.ImportFrom, module: Module) -> str:
    """The absolute module a ``from`` import names, relative levels resolved."""
    if not node.level:
        return node.module or ""
    package = module.name if module.is_package else module.name.rpartition(".")[0]
    parts = package.split(".")
    base = parts[: len(parts) - (node.level - 1)]
    return ".".join(base + ([node.module] if node.module else []))


@dataclass(frozen=True)
class Import:
    name: str
    line: int


def imports(module: Module) -> list[Import]:
    """Every module ``module`` imports, at any depth (function-local imports too).

    A ``from a import b`` yields both ``a`` and ``a.b``, since ``b`` may be a module.
    """
    found: list[Import] = []
    for node in ast.walk(module.tree):
        if isinstance(node, ast.Import):
            found += [Import(alias.name, node.lineno) for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            base = _from_base(node, module)
            found.append(Import(base, node.lineno))
            found += [Import(f"{base}.{a.name}", node.lineno) for a in node.names]
    return found


def names_module(name: str, target: str) -> bool:
    """Whether the dotted ``name`` is ``target`` or inside it."""
    return name == target or name.startswith(target + ".")


def dotted(node: ast.expr) -> str | None:
    """``a.b.c`` for a name or an attribute chain, else None."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        head = dotted(node.value)
        return None if head is None else f"{head}.{node.attr}"
    return None


def calls(module: Module) -> Iterator[tuple[str, ast.Call]]:
    """Every call whose callee is a name or an attribute chain, with that chain."""
    for node in ast.walk(module.tree):
        if isinstance(node, ast.Call) and (name := dotted(node.func)) is not None:
            yield name, node


def import_all() -> list[ModuleType]:
    """Import every module of the package (but the migrations), so every registry a
    module fills at import (``SINK_SPECS``, an event or job registry) is complete."""
    loaded: list[ModuleType] = []
    for module in modules():
        if any(names_module(module.name, skip) for skip in NOT_IMPORTED):
            continue
        loaded.append(importlib.import_module(module.name))
    return loaded


def attributes_named(attribute: str) -> dict[str, Any]:
    """``{module name: value}`` for every loaded module defining ``attribute`` itself
    (not one it imported from another module)."""
    found: dict[str, Any] = {}
    for module in import_all():
        value = getattr(module, attribute, None)
        if value is None:
            continue
        # A module that imported the registry holds the same object; keep the owner.
        if any(value is seen for seen in found.values()):
            continue
        found[module.__name__] = value
    return found


def event_registries() -> dict[str, type[BaseModel]]:
    """Every event type any module's ``EVENT_MODELS`` registers, refusing a type two
    registries both claim."""
    merged: dict[str, type[BaseModel]] = {}
    for owner, registry in attributes_named("EVENT_MODELS").items():
        assert isinstance(registry, Mapping), f"{owner}.EVENT_MODELS is not a mapping"
        for event_type, model in registry.items():
            assert event_type not in merged, f"{event_type} is registered twice"
            assert isinstance(model, type) and issubclass(model, BaseModel), event_type
            merged[event_type] = model
    return merged


def job_names() -> list[str]:
    """Every job name any module's ``JOBS`` declares: the keys of a mapping, or the
    strings or ``.name`` of each item of a collection."""
    names: list[str] = []
    for owner, jobs in attributes_named("JOBS").items():
        items = list(jobs.keys() if isinstance(jobs, Mapping) else jobs)
        for item in items:
            name = item if isinstance(item, str) else getattr(item, "name", None)
            assert isinstance(name, str), f"{owner}.JOBS holds {item!r}, not a name"
            names.append(name)
    return names


def normalized_package(name: str) -> str:
    """A distribution name as PEP 503 compares it."""
    return re.sub(r"[-_.]+", "-", name).lower()


def _requirement_name(requirement: str) -> str:
    match = re.match(r"[A-Za-z0-9][A-Za-z0-9._-]*", requirement.strip())
    return normalized_package(match[0]) if match else ""


def declared_packages(pyproject: Path = PYPROJECT, lock: Path = UV_LOCK) -> set[str]:
    """Every distribution ``pyproject.toml`` declares (dependencies, optional
    dependencies, dependency groups) and every package ``uv.lock`` pins, normalized."""
    project = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    requirements: list[str] = list(project.get("project", {}).get("dependencies", []))
    for extra in project.get("project", {}).get("optional-dependencies", {}).values():
        requirements += extra
    for group in project.get("dependency-groups", {}).values():
        requirements += [each for each in group if isinstance(each, str)]
    names = {_requirement_name(each) for each in requirements}
    if lock.exists():
        pinned = tomllib.loads(lock.read_text(encoding="utf-8")).get("package", [])
        names |= {normalized_package(each["name"]) for each in pinned}
    return names
