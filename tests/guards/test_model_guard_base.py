"""AC-48a, the base: no Anthropic SDK and no API key.

Every model call goes through ``claude -p`` on the owner's subscription, never a
provider SDK. So no module in ``src`` or ``scripts`` imports ``anthropic`` or another
Anthropic or Claude SDK, ``pyproject.toml`` and ``uv.lock`` name none, and no module
reads an environment variable whose name holds ``ANTHROPIC`` or ``API_KEY``
(``os.environ[...]``, ``os.environ.get``, ``os.getenv``, ``environ.setdefault``,
``environ.pop`` or ``in os.environ``).
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from .source_tree import (
    PACKAGE,
    PYPROJECT,
    SCRIPTS,
    UV_LOCK,
    Module,
    calls,
    copy_package,
    declared_packages,
    imports,
    modules,
    plant,
)

SDK_PREFIXES = (
    "anthropic",
    "claude-agent-sdk",
    "claude-code-sdk",
    "langchain-anthropic",
)
SDK_IMPORTS = (
    "anthropic",
    "claude_agent_sdk",
    "claude_code_sdk",
    "langchain_anthropic",
)
KEY_WORDS = ("ANTHROPIC", "API_KEY")
ENV_READERS = {"get", "getenv", "setdefault", "pop", "getenvb"}


def is_sdk_distribution(name: str) -> bool:
    return any(name == each or name.startswith(each + "-") for each in SDK_PREFIXES)


def sdk_distributions(pyproject: Path = PYPROJECT, lock: Path = UV_LOCK) -> list[str]:
    return sorted(
        each for each in declared_packages(pyproject, lock) if is_sdk_distribution(each)
    )


def sdk_imports(*roots: Path) -> list[str]:
    return sorted(
        {
            f"{module.name}: {each.name.split('.')[0]}"
            for root in roots
            for module in modules(root)
            for each in imports(module)
            if each.name.split(".")[0].startswith(SDK_IMPORTS)
        }
    )


def _names_a_key(node: ast.expr | None) -> bool:
    return (
        isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and any(word in node.value.upper() for word in KEY_WORDS)
    )


def _is_environ(node: ast.expr) -> bool:
    return (isinstance(node, ast.Name) and node.id == "environ") or (
        isinstance(node, ast.Attribute) and node.attr in {"environ", "environb"}
    )


def key_reads(module: Module) -> list[str]:
    found: list[int] = []
    for name, call in calls(module):
        last = name.rpartition(".")[2]
        if last in {"getenv", "getenvb"} and call.args and _names_a_key(call.args[0]):
            found.append(call.lineno)
        elif (
            last in ENV_READERS
            and isinstance(call.func, ast.Attribute)
            and _is_environ(call.func.value)
            and call.args
            and _names_a_key(call.args[0])
        ):
            found.append(call.lineno)
    for node in ast.walk(module.tree):
        if (
            isinstance(node, ast.Subscript)
            and isinstance(node.ctx, ast.Load)
            and _is_environ(node.value)
            and _names_a_key(node.slice)
        ):
            found.append(node.lineno)
        elif (
            isinstance(node, ast.Compare)
            and any(isinstance(op, ast.In | ast.NotIn) for op in node.ops)
            and _names_a_key(node.left)
            and any(_is_environ(each) for each in node.comparators)
        ):
            found.append(node.lineno)
    return [f"{module.name}:{line} reads a key variable" for line in sorted(found)]


def all_key_reads(*roots: Path) -> list[str]:
    return [found for root in roots for m in modules(root) for found in key_reads(m)]


def test_no_module_imports_an_anthropic_sdk() -> None:
    assert sdk_imports(PACKAGE, SCRIPTS) == []


def test_no_anthropic_sdk_is_declared_or_locked() -> None:
    assert sdk_distributions() == []


def test_no_module_reads_an_api_key_variable() -> None:
    assert all_key_reads(PACKAGE, SCRIPTS) == []
    # The control: the scan sees an ordinary environment read and does not count it.
    db = next(module for module in modules() if module.name == "rheo_finance.db")
    assert any(name == "os.environ.get" for name, _ in calls(db))


def test_a_planted_sdk_import_and_dependency_are_found(tmp_path: Path) -> None:
    root = copy_package(tmp_path)
    plant(
        root,
        "model/planted.py",
        "import anthropic\ndef later():\n    from claude_agent_sdk import query\n",
    )
    assert sdk_imports(root) == [
        "rheo_finance.model.planted: anthropic",
        "rheo_finance.model.planted: claude_agent_sdk",
    ]
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        '[project]\nname = "x"\ndependencies = ["Anthropic>=0.40"]\n', encoding="utf-8"
    )
    lock = tmp_path / "uv.lock"
    lock.write_text(
        '[[package]]\nname = "anthropic"\nversion = "1"\n'
        '[[package]]\nname = "anthropic-bedrock"\nversion = "1"\n',
        encoding="utf-8",
    )
    assert sdk_distributions(pyproject, lock) == ["anthropic", "anthropic-bedrock"]


@pytest.mark.parametrize(
    "source",
    [
        "import os\nkey = os.environ['ANTHROPIC_API_KEY']\n",
        "import os\nkey = os.environ.get('ANTHROPIC_API_KEY')\n",
        "import os\nkey = os.getenv('WISE_API_KEY')\n",
        "from os import environ\nkey = environ.get('anthropic_base_url')\n",
        "from os import getenv\nkey = getenv('KRAKEN_API_KEY', '')\n",
        "import os\nhas = 'ANTHROPIC_API_KEY' in os.environ\n",
        "import os\nkey = os.environ.pop('STRIPE_API_KEY', None)\n",
    ],
)
def test_a_planted_key_read_is_found(source: str, tmp_path: Path) -> None:
    root = copy_package(tmp_path)
    plant(root, "model/planted.py", source)
    assert all_key_reads(root) == ["rheo_finance.model.planted:2 reads a key variable"]


def test_an_ordinary_environment_read_is_not_counted(tmp_path: Path) -> None:
    root = copy_package(tmp_path)
    plant(
        root,
        "model/planted.py",
        "import os\ndata = os.environ.get('FINANCE_DATA_DIR')\n"
        "env = {k: v for k, v in os.environ.items() if k != 'ANTHROPIC_API_KEY'}\n",
    )
    assert all_key_reads(root) == []
