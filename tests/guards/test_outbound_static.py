"""AC-45a and AC-45b: every outbound path that exists is a reviewed read, or one of the
two sinks' declared writes.

The guard is keyed on the method and on the reviewed table of vendor-documented paths,
``src/rheo_finance/sinks/read_paths.toml``, not on a banned-word list: vendors document
read paths that name transfers or orders, and those are fine to read.

(a) Only the modules in ``OUTBOUND_ALLOWED`` import ``urllib``, ``imaplib``,
    ``subprocess``, ``socket`` or another network or process module, or call a process
    spawner such as ``os.system``. ``urllib.parse`` is exempt: it parses text and opens
    nothing. ``scripts/`` is scanned too.
(b) Every allowlist entry that exists is a row of the table. The entries are the
    requests of every ``SinkSpec`` the providers in ``SINK_SPECS`` return, and every
    connector's ``READ_ALLOWLIST``. That is the whole set: ``http.Client`` refuses an
    allowlist entry its spec does not declare, and the gate refuses a spec whose digest
    is not the approved digest of the provider's spec. A row is refused when it is not
    a GET and carries no ``exception``, when it carries ``exception`` for a sink other
    than ``mot`` or ``telegram``, and when its documentation URL is empty.
(c) No ``Client(...)`` outside the tests passes ``allowed_schemes`` holding ``http``
    (or anything that is not a literal).
(d) No browser-automation package is in ``pyproject.toml`` or ``uv.lock``, and no module
    imports one.
(e) Every connector module (``rheo_finance.connectors.*`` and
    ``rheo_finance.features.connectors_api.*``) declares ``SINK_SPEC`` (a ``SinkSpec``)
    and ``READ_ALLOWLIST`` (``(method, path)`` pairs the spec declares), makes no write
    call (a ``request`` whose method is not a literal ``GET``, or a ``post``, ``put``,
    ``patch`` or ``delete`` call), and names no URL on a host outside
    ``SINK_SPEC.hosts``.

The lists here are constants later prompts extend: a module that gains a subprocess call
joins ``OUTBOUND_ALLOWED`` with its reason, and a sink request or connector entry adds
its table row, in the same change.
"""

from __future__ import annotations

import ast
import importlib
import importlib.util
import re
import tomllib
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from urllib.parse import urlsplit

import pytest
from sqlalchemy import Connection

from rheo_finance.context import UnitOfWork
from rheo_finance.settings import SINKS
from rheo_finance.sinks import spec as sink_spec
from rheo_finance.sinks.spec import METHODS, RequestTemplate, SinkSpec

from .source_tree import (
    PACKAGE,
    PYPROJECT,
    SCRIPTS,
    UV_LOCK,
    Module,
    calls,
    copy_package,
    declared_packages,
    import_all,
    imports,
    modules,
    names_module,
    normalized_package,
    plant,
)

READ_PATHS = PACKAGE / "sinks" / "read_paths.toml"

# --- (a) who may reach the network or start a process ---------------------------------

OUTBOUND_IMPORTS = (
    "urllib", "imaplib", "subprocess", "socket", "http.client", "smtplib", "ftplib",
    "poplib", "telnetlib", "multiprocessing", "pty",
)  # fmt: skip
OUTBOUND_EXEMPT = ("urllib.parse",)
PROCESS_CALL = re.compile(
    r"os\.(system|popen|exec\w*|spawn\w*|posix_spawnp?|fork\w*)"
    r"|asyncio\.create_subprocess_\w+"
)

# Each module that may, and the one reason. Later prompts add theirs here.
OUTBOUND_ALLOWED: dict[str, str] = {
    "rheo_finance.http": "urllib and http.client: the one HTTP choke point",
    "rheo_finance.mail.imap": "imaplib: the read-only IMAP source",
    "rheo_finance.model.claude_cli": "subprocess: claude -p with tools disabled",
    "rheo_finance.jobs.backup": "subprocess: pg_dump, fdesetup, diskutil, rsync",
    "rheo_finance.jobs.agent": "subprocess: launchctl bootstrap and bootout",
    "rheo_finance.db": "subprocess: docker compose up and docker exec (db up, init,"
    " restore)",
    "rheo_finance.parsers.docextract_adapter": "subprocess via docextract: pdftotext",
    "scripts.repo_check": "subprocess: git ls-files and git diff --cached, local and"
    " read-only",
}


def outbound_modules(*roots: Path) -> dict[str, list[str]]:
    """``{module: [what it reaches]}`` for every module that reaches the network or
    starts a process, over each root."""
    found: dict[str, list[str]] = {}
    for root in roots:
        for module in modules(root):
            how = [
                f"imports {each.name}"
                for each in imports(module)
                if any(names_module(each.name, target) for target in OUTBOUND_IMPORTS)
                and not any(names_module(each.name, ok) for ok in OUTBOUND_EXEMPT)
            ]
            how += [
                f"calls {name}"
                for name, _ in calls(module)
                if PROCESS_CALL.fullmatch(name)
            ]
            how += [
                f"imports {each.name}"
                for each in imports(module)
                if PROCESS_CALL.fullmatch(each.name)
            ]
            if how:
                found[module.name] = sorted(set(how))
    return found


def test_only_the_allowed_modules_reach_the_network_or_start_a_process() -> None:
    found = outbound_modules(PACKAGE, SCRIPTS)
    assert set(found) - set(OUTBOUND_ALLOWED) == set(), found
    # The control: the modules that do so today are found, so the scan is not blind.
    assert {"rheo_finance.http", "rheo_finance.db", "scripts.repo_check"} <= set(found)


@pytest.mark.parametrize(
    "source",
    [
        "import subprocess\n",
        "from subprocess import run\n",
        "import urllib.request\n",
        "from urllib import request\n",
        "import socket\n",
        "import imaplib\n",
        "import http.client\n",
        "from http import client\n",
        "import os\nos.system('x')\n",
        "from os import system\n",
        "import asyncio\nasyncio.create_subprocess_exec('x')\n",
        "def later():\n    import subprocess\n",
    ],
)
def test_a_planted_outbound_module_is_found(source: str, tmp_path: Path) -> None:
    root = copy_package(tmp_path)
    plant(root, "rates/planted.py", source)
    found = outbound_modules(root)
    assert "rheo_finance.rates.planted" in found
    assert set(found) - set(OUTBOUND_ALLOWED) == {"rheo_finance.rates.planted"}


def test_parsing_a_url_is_not_outbound(tmp_path: Path) -> None:
    root = copy_package(tmp_path)
    plant(root, "rates/parse.py", "from urllib.parse import urlsplit\n")
    assert "rheo_finance.rates.parse" not in outbound_modules(root)


# --- (b) the reviewed table of read paths ---------------------------------------------

EXCEPTION_SINKS = frozenset({"mot", "telegram"})
ROW_KEYS = {"sink", "method", "path", "docs", "why", "exception"}


@dataclass(frozen=True)
class ReadPath:
    sink: str
    method: str
    path: str
    docs: str
    why: str
    exception: bool


@dataclass(frozen=True)
class Entry:
    """One allowlist entry that exists, and where it came from."""

    sink: str
    method: str
    path: str
    origin: str


def load_table(text: str) -> tuple[list[ReadPath], list[str]]:
    """The rows, and every problem with them."""
    raw = tomllib.loads(text)
    problems = [
        f"unexpected top-level key {key!r}" for key in raw if key != "read_path"
    ]
    rows: list[ReadPath] = []
    listed = raw.get("read_path", [])
    if not isinstance(listed, list):
        return rows, [*problems, "read_path is not an array of tables"]
    for index, row in enumerate(listed):
        where = f"row {index + 1}"
        problems += [f"{where}: unexpected key {key!r}" for key in set(row) - ROW_KEYS]
        missing = {"sink", "method", "path", "docs", "why"} - set(row)
        if missing:
            problems.append(f"{where}: missing {', '.join(sorted(missing))}")
            continue
        exception = row.get("exception", False)
        if not all(isinstance(row[key], str) for key in ROW_KEYS - {"exception"}):
            problems.append(f"{where}: a field is not text")
            continue
        if not isinstance(exception, bool):
            problems.append(f"{where}: exception is true or false")
            continue
        rows.append(
            ReadPath(
                row["sink"],
                row["method"],
                row["path"],
                row["docs"].strip(),
                row["why"].strip(),
                exception,
            )
        )
    return rows, problems + row_problems(rows)


def row_problems(rows: Iterable[ReadPath]) -> list[str]:
    problems: list[str] = []
    seen: set[tuple[str, str, str]] = set()
    for row in rows:
        where = f"{row.sink} {row.method} {row.path}"
        key = (row.sink, row.method, row.path)
        if key in seen:
            problems.append(f"{where}: listed twice")
        seen.add(key)
        if row.sink not in SINKS:
            problems.append(f"{where}: {row.sink!r} is not a sink")
        if row.method not in METHODS:
            problems.append(f"{where}: {row.method!r} is not an HTTP method")
        try:
            RequestTemplate(method="GET", path=row.path)
        except ValueError:
            problems.append(f"{where}: the path is not a request template path")
        if not row.docs:
            problems.append(f"{where}: no documentation URL")
        elif urlsplit(row.docs).scheme != "https" or not urlsplit(row.docs).hostname:
            problems.append(f"{where}: the documentation URL is not an https URL")
        if not row.why:
            problems.append(f"{where}: no line saying why it is read-only")
        if row.exception and row.sink not in EXCEPTION_SINKS:
            problems.append(f"{where}: only mot and telegram may carry exception")
        if row.method != "GET" and not row.exception:
            problems.append(f"{where}: a non-GET row without exception")
    return problems


def entry_problems(entries: Iterable[Entry], rows: Iterable[ReadPath]) -> list[str]:
    table = {(row.sink, row.method, row.path) for row in rows}
    return sorted(
        f"{entry.origin}: {entry.sink} {entry.method} {entry.path} is not a row of"
        " read_paths.toml"
        for entry in entries
        if (entry.sink, entry.method, entry.path) not in table
    )


def spec_entries(spec: SinkSpec, origin: str) -> list[Entry]:
    return [Entry(spec.name, each.method, each.path, origin) for each in spec.requests]


def provider_entries(uow: UnitOfWork) -> list[Entry]:
    """The requests of every spec a registered provider returns now."""
    import_all()
    entries: list[Entry] = []
    for sink, provider in sorted(sink_spec.SINK_SPECS.items()):
        spec = provider(uow)
        assert spec.name == sink, f"the provider for {sink} returned {spec.name}"
        entries += spec_entries(spec, f"SINK_SPECS[{sink!r}]")
    return entries


def test_the_table_is_well_formed() -> None:
    _rows, problems = load_table(READ_PATHS.read_text(encoding="utf-8"))
    assert problems == []


def test_every_allowlist_entry_is_a_row_of_the_table(owner_conn: Connection) -> None:
    rows, _ = load_table(READ_PATHS.read_text(encoding="utf-8"))
    entries = provider_entries(UnitOfWork(owner_conn))
    entries += [
        entry
        for connector in connector_modules(PACKAGE)
        for entry in connector_entries(connector, load_module(connector))
    ]
    assert entry_problems(entries, rows) == []


GOOD_ROW = (
    '[[read_path]]\nsink = "rates"\nmethod = "GET"\npath = "/v1/rates/:day"\n'
    'docs = "https://vendor.invalid/docs/rates"\nwhy = "Reads published rates."\n'
)


@pytest.mark.parametrize(
    ("row", "problem"),
    [
        (
            '[[read_path]]\nsink = "rates"\nmethod = "POST"\npath = "/v1/order"\n'
            'docs = "https://vendor.invalid/docs"\nwhy = "x"\n',
            "a non-GET row without exception",
        ),
        (
            '[[read_path]]\nsink = "kraken"\nmethod = "POST"\npath = "/v1/withdraw"\n'
            'docs = "https://vendor.invalid/docs"\nwhy = "x"\nexception = true\n',
            "only mot and telegram may carry exception",
        ),
        (
            '[[read_path]]\nsink = "rates"\nmethod = "GET"\npath = "/v1/rates"\n'
            'docs = ""\nwhy = "x"\n',
            "no documentation URL",
        ),
        (
            '[[read_path]]\nsink = "rates"\nmethod = "GET"\npath = "/v1/rates"\n'
            'docs = "   "\nwhy = "x"\n',
            "no documentation URL",
        ),
        (
            '[[read_path]]\nsink = "rates"\nmethod = "GET"\npath = "/v1/rates"\n'
            'why = "x"\n',
            "missing docs",
        ),
        (
            '[[read_path]]\nsink = "rates"\nmethod = "GET"\npath = "/v1/rates"\n'
            'docs = "http://vendor.invalid/docs"\nwhy = "x"\n',
            "not an https URL",
        ),
        (
            '[[read_path]]\nsink = "bank"\nmethod = "GET"\npath = "/v1/rates"\n'
            'docs = "https://vendor.invalid/docs"\nwhy = "x"\n',
            "is not a sink",
        ),
        (GOOD_ROW + GOOD_ROW, "listed twice"),
    ],
)
def test_a_bad_table_row_is_refused(row: str, problem: str) -> None:
    _rows, problems = load_table(row)
    assert any(problem in each for each in problems), problems


def test_the_mot_and_telegram_writes_may_carry_exception() -> None:
    text = (
        '[[read_path]]\nsink = "mot"\nmethod = "POST"\npath = "/api/tickets"\n'
        'docs = "https://vendor.invalid/mot"\nwhy = "Creates a reminder ticket."\n'
        "exception = true\n"
        '[[read_path]]\nsink = "telegram"\nmethod = "POST"\npath = "/api/mcp"\n'
        'docs = "https://vendor.invalid/mcp"\nwhy = "The notify tool over JSON-RPC."\n'
        "exception = true\n"
    )
    rows, problems = load_table(text + GOOD_ROW)
    assert problems == [] and len(rows) == 3


def planted_provider(_uow: UnitOfWork) -> SinkSpec:
    return SinkSpec(
        name="rates",
        hosts=("vendor.invalid",),
        requests=(
            RequestTemplate(method="GET", path="/v1/rates/:day"),
            RequestTemplate(method="POST", path="/v1/transfers"),
        ),
        key_file=None,
        payload_policy_version=0,
    )


def test_a_provider_request_outside_the_table_is_found(
    owner_conn: Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    import_all()  # first, so no module registers into the planted registry
    monkeypatch.setattr(sink_spec, "SINK_SPECS", {"rates": planted_provider})
    rows, _ = load_table(GOOD_ROW)
    problems = entry_problems(provider_entries(UnitOfWork(owner_conn)), rows)
    assert problems == [
        "SINK_SPECS['rates']: rates POST /v1/transfers is not a row of read_paths.toml"
    ]


# --- (c) plain http only in tests -----------------------------------------------------


def plain_http_clients(*roots: Path) -> list[str]:
    found: list[str] = []
    for root in roots:
        for module in modules(root):
            for name, call in calls(module):
                if name.rpartition(".")[2] != "Client":
                    continue
                for keyword in call.keywords:
                    if keyword.arg is None:
                        found.append(f"{module.name}:{call.lineno} Client(**...)")
                    elif keyword.arg == "allowed_schemes":
                        found += _scheme_problems(module, call, keyword.value)
    return found


def _scheme_problems(module: Module, call: ast.Call, value: ast.expr) -> list[str]:
    where = f"{module.name}:{call.lineno}"
    if not isinstance(value, ast.Tuple | ast.List | ast.Set) or not all(
        isinstance(each, ast.Constant) for each in value.elts
    ):
        return [f"{where} allowed_schemes is not a literal"]
    if any(
        each.value == "http" for each in value.elts if isinstance(each, ast.Constant)
    ):
        return [f"{where} allowed_schemes holds http"]
    return []


def test_no_client_outside_the_tests_allows_plain_http() -> None:
    assert plain_http_clients(PACKAGE, SCRIPTS) == []


def test_a_planted_plain_http_client_is_found(tmp_path: Path) -> None:
    root = copy_package(tmp_path)
    plant(
        root,
        "rates/planted.py",
        "from rheo_finance import http\n"
        "a = http.Client('rates', s, (), uow=u, allowed_schemes=('https', 'http'))\n"
        "b = Client('rates', spec, (), uow=uow, allowed_schemes=SCHEMES)\n"
        "c = Client('rates', spec, (), uow=uow, allowed_schemes=('https',))\n",
    )
    assert plain_http_clients(root) == [
        "rheo_finance.rates.planted:2 allowed_schemes holds http",
        "rheo_finance.rates.planted:3 allowed_schemes is not a literal",
    ]


# --- (d) no browser automation --------------------------------------------------------

BROWSER_PACKAGES = (
    "playwright", "selenium", "seleniumbase", "puppeteer", "pyppeteer", "mechanize",
    "mechanicalsoup", "splinter", "helium", "robobrowser", "requests-html",
    "undetected-chromedriver", "webdriver-manager", "chromedriver-autoinstaller",
    "browser-use", "nodriver", "zendriver", "botasaurus", "pyautogui", "pywinauto",
)  # fmt: skip


def is_browser_package(name: str) -> bool:
    name = normalized_package(name)
    return any(name == each or name.startswith(each + "-") for each in BROWSER_PACKAGES)


def browser_packages(pyproject: Path, lock: Path) -> list[str]:
    return sorted(
        each for each in declared_packages(pyproject, lock) if is_browser_package(each)
    )


def browser_imports(root: Path) -> list[str]:
    return sorted(
        {
            f"{module.name}: {top}"
            for module in modules(root)
            for each in imports(module)
            if is_browser_package(top := each.name.split(".")[0])
        }
    )


def test_no_browser_automation_package_is_declared_or_locked() -> None:
    assert browser_packages(PYPROJECT, UV_LOCK) == []
    # The control: the scan reads both files.
    assert {"sqlalchemy", "pytest", "doc-extraction-api"} <= declared_packages()


def test_no_module_imports_browser_automation() -> None:
    assert browser_imports(PACKAGE) == []


def test_a_planted_browser_package_is_found(tmp_path: Path) -> None:
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        '[project]\nname = "x"\ndependencies = ["sqlalchemy>=2", "Playwright>=1"]\n'
        '[dependency-groups]\ndev = ["selenium_wire==5"]\n',
        encoding="utf-8",
    )
    lock = tmp_path / "uv.lock"
    lock.write_text(
        '[[package]]\nname = "pyppeteer"\nversion = "2"\n'
        '[[package]]\nname = "psycopg"\nversion = "3"\n',
        encoding="utf-8",
    )
    assert browser_packages(pyproject, lock) == [
        "playwright",
        "pyppeteer",
        "selenium-wire",
    ]
    root = copy_package(tmp_path)
    plant(root, "planted.py", "from playwright.sync_api import sync_playwright\n")
    assert browser_imports(root) == ["rheo_finance.planted: playwright"]


# --- (e) connectors -------------------------------------------------------------------

CONNECTOR_PACKAGES = ("rheo_finance.connectors", "rheo_finance.features.connectors_api")
WRITE_CALLS = {"post", "put", "patch", "delete"}
_URL = re.compile(r"\bhttps?://[^\s'\"<>]+", re.IGNORECASE)


def connector_modules(root: Path) -> list[Module]:
    """Every module inside a connector package, the package files themselves too."""
    return [
        module
        for module in modules(root)
        if any(names_module(module.name, package) for package in CONNECTOR_PACKAGES)
    ]


def load_module(module: Module) -> ModuleType:
    """The real module when it is in the package, else the file loaded on its own."""
    if module.path.is_relative_to(PACKAGE):
        return importlib.import_module(module.name)
    spec = importlib.util.spec_from_file_location(
        "_guard_fixture_" + module.name.replace(".", "_"), module.path
    )
    assert spec is not None and spec.loader is not None
    loaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)
    return loaded


def connector_entries(module: Module, loaded: ModuleType) -> list[Entry]:
    spec = getattr(loaded, "SINK_SPEC", None)
    allowlist = getattr(loaded, "READ_ALLOWLIST", None)
    if not isinstance(spec, SinkSpec) or not isinstance(allowlist, tuple):
        return []
    entries = spec_entries(spec, f"{module.name}.SINK_SPEC")
    entries += [
        Entry(spec.name, method, path, f"{module.name}.READ_ALLOWLIST")
        for method, path in allowlist
    ]
    return entries


def connector_problems(
    module: Module, loaded: ModuleType, rows: Iterable[ReadPath]
) -> list[str]:
    where = module.name
    problems: list[str] = []
    spec = getattr(loaded, "SINK_SPEC", None)
    allowlist = getattr(loaded, "READ_ALLOWLIST", None)
    if module.is_package and spec is None and allowlist is None:
        hosts: tuple[str, ...] = ()
    elif not isinstance(spec, SinkSpec):
        return [f"{where}: declares no SINK_SPEC"]
    elif not (
        isinstance(allowlist, tuple)
        and all(
            isinstance(pair, tuple)
            and len(pair) == 2
            and all(isinstance(p, str) for p in pair)
            for pair in allowlist
        )
    ):
        return [f"{where}: declares no READ_ALLOWLIST of (method, path) pairs"]
    else:
        hosts = spec.hosts
        declared = {(each.method, each.path) for each in spec.requests}
        problems += [
            f"{where}: {method} {path} is not a request SINK_SPEC declares"
            for method, path in allowlist
            if (method, path) not in declared
        ]
        problems += entry_problems(connector_entries(module, loaded), rows)
    problems += write_calls(module)
    problems += [
        f"{where}:{line} names host {host}, outside SINK_SPEC.hosts"
        for line, host in named_hosts(module)
        if host not in hosts
    ]
    return problems


def write_calls(module: Module) -> list[str]:
    found: list[str] = []
    for name, call in calls(module):
        last = name.rpartition(".")[2]
        where = f"{module.name}:{call.lineno}"
        if last in WRITE_CALLS:
            found.append(f"{where} write call {name}(")
        elif last in {"request", "urlopen", "Request"}:
            method = _method_argument(call, last)
            if method != "GET":
                found.append(f"{where} {name}( with method {method or 'not a literal'}")
    return found


def _literal(node: ast.expr) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _method_argument(call: ast.Call, callee: str) -> str | None:
    """The literal method a request call names, or None when it is not a literal.
    ``urlopen`` and ``Request`` send a POST when given data, else a GET."""
    for keyword in call.keywords:
        if keyword.arg == "method":
            return _literal(keyword.value)
    if callee == "request":
        return _literal(call.args[0]) if call.args else None
    has_data = any(k.arg == "data" for k in call.keywords) or len(call.args) > 1
    return "POST" if has_data else "GET"


def named_hosts(module: Module) -> list[tuple[int, str]]:
    """Each ``host[:port]`` a URL string in the module names."""
    found: list[tuple[int, str]] = []
    for node in ast.walk(module.tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            for url in _URL.findall(node.value):
                parts = urlsplit(url)
                if parts.hostname:
                    port = f":{parts.port}" if parts.port else ""
                    found.append((node.lineno, f"{parts.hostname}{port}"))
    return found


def check_connectors(root: Path, rows: list[ReadPath]) -> dict[str, list[str]]:
    return {
        module.name: problems
        for module in connector_modules(root)
        if (problems := connector_problems(module, load_module(module), rows))
    }


def test_every_connector_present_declares_a_read_allowlist() -> None:
    rows, _ = load_table(READ_PATHS.read_text(encoding="utf-8"))
    assert check_connectors(PACKAGE, rows) == {}


CONNECTOR = """
from rheo_finance.sinks.spec import RequestTemplate, SinkSpec

SINK_SPEC = SinkSpec(
    name="wise",
    hosts=("api.vendor.invalid",),
    requests=(RequestTemplate(method="GET", path="/v1/profiles/:id/transfers"),),
    key_file="keys/wise.txt",
    payload_policy_version=0,
)
READ_ALLOWLIST = (("GET", "/v1/profiles/:id/transfers"),)
BASE = "https://api.vendor.invalid/v1"


def transfers(client, profile):
    return client.request("GET", BASE + "/profiles/" + profile + "/transfers")
"""
CONNECTOR_ROWS = (
    '[[read_path]]\nsink = "wise"\nmethod = "GET"\n'
    'path = "/v1/profiles/:id/transfers"\n'
    'docs = "https://vendor.invalid/docs/transfers"\n'
    'why = "Lists past transfers; the endpoint reads only."\n'
)


def _connector_tree(tmp_path: Path, source: str, package: str) -> Path:
    root = copy_package(tmp_path)
    relative = package.removeprefix("rheo_finance.").replace(".", "/")
    plant(root, f"{relative}/wise.py", source)
    return root


@pytest.mark.parametrize("package", CONNECTOR_PACKAGES)
def test_a_well_formed_fixture_connector_passes(tmp_path: Path, package: str) -> None:
    # The control: the check finds the fixture and has nothing to say about it.
    root = _connector_tree(tmp_path, CONNECTOR, package)
    rows, _ = load_table(CONNECTOR_ROWS)
    assert f"{package}.wise" in [module.name for module in connector_modules(root)]
    assert check_connectors(root, rows) == {}


@pytest.mark.parametrize(
    ("change", "problem"),
    [
        (
            ('return client.request("GET"', 'return client.request("POST"'),
            "request( with method POST",
        ),
        (
            ("return client.request(", "client.post(BASE)\n    return client.request("),
            "write call client.post(",
        ),
        (
            ('return client.request("GET"', "return client.request(verb"),
            "request( with method not a literal",
        ),
        (
            (
                'BASE = "https://api.vendor.invalid/v1"',
                'BASE = "https://other.invalid/v1"',
            ),
            "names host other.invalid, outside SINK_SPEC.hosts",
        ),
        (
            ("READ_ALLOWLIST = ", "_UNUSED = "),
            "declares no READ_ALLOWLIST",
        ),
        (
            ("SINK_SPEC = SinkSpec(", "_SPEC = SinkSpec("),
            "declares no SINK_SPEC",
        ),
        (
            (
                'READ_ALLOWLIST = (("GET", "/v1/profiles/:id/transfers"),)',
                'READ_ALLOWLIST = (("GET", "/v1/profiles/:id/transfers"),'
                ' ("POST", "/v1/transfers"))',
            ),
            "POST /v1/transfers is not a request SINK_SPEC declares",
        ),
    ],
)
def test_a_planted_violation_in_a_fixture_connector_is_found(
    tmp_path: Path, change: tuple[str, str], problem: str
) -> None:
    old, new = change
    assert old in CONNECTOR
    root = _connector_tree(tmp_path, CONNECTOR.replace(old, new), CONNECTOR_PACKAGES[0])
    rows, _ = load_table(CONNECTOR_ROWS)
    found = check_connectors(root, rows)
    assert any(
        problem in each for each in found.get(f"{CONNECTOR_PACKAGES[0]}.wise", [])
    )


def test_a_connector_request_outside_the_table_is_found(tmp_path: Path) -> None:
    root = _connector_tree(tmp_path, CONNECTOR, CONNECTOR_PACKAGES[0])
    found = check_connectors(root, [])
    assert found[f"{CONNECTOR_PACKAGES[0]}.wise"] == [
        f"{CONNECTOR_PACKAGES[0]}.wise.READ_ALLOWLIST: wise GET"
        " /v1/profiles/:id/transfers is not a row of read_paths.toml",
        f"{CONNECTOR_PACKAGES[0]}.wise.SINK_SPEC: wise GET"
        " /v1/profiles/:id/transfers is not a row of read_paths.toml",
    ]
