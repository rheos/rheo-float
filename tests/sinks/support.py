"""Synthetic Current operations server with identity folding and revision CAS."""

from __future__ import annotations

import json
import threading
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Literal, NotRequired, TypedDict

from pydantic import ConfigDict, TypeAdapter, ValidationError, with_config


@with_config(ConfigDict(extra="forbid"))
class WireFields(TypedDict, total=False):
    """Fields accepted and returned by the four synthetic Current operations."""

    ref: str
    revision: int
    title: str
    body: str
    due_on: str | None
    priority: Literal["critical", "high", "normal", "low"]
    status: Literal["open", "waiting", "snoozed", "done"]
    area: str
    provenance: str
    source_kind: str
    source_ref: str
    fingerprint: str
    clear: list[Literal["due_on"]]


class WireResult(TypedDict, total=False):
    action: str
    ref: str
    item: WireFields


class WireError(TypedDict):
    error_code: str
    error_text: str


class WireEnvelope(TypedDict):
    state: Literal["succeeded", "failed"]
    operation_id: str | None
    result: WireResult | None
    error: NotRequired[WireError]


REQUEST = TypeAdapter(WireFields)


class CurrentStub:
    def __init__(self) -> None:
        self.requests: list[tuple[str, WireFields]] = []
        self.items: dict[str, WireFields] = {}
        self.identities: dict[tuple[str, str], str] = {}
        self.fingerprints: dict[str, set[str]] = {}
        self.drop_create = False
        self.ref_only = False
        self.operation_id: str | None = None
        self.unauthorized = False
        self.refuse_operation: str | None = None
        self.refuse_source_ref: str | None = None
        self.stale = 0
        self.stale_http_status = 409
        self.before_create: Callable[[], None] | None = None
        self.before_mutation: Callable[[str, WireFields], None] | None = None
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                try:
                    body = REQUEST.validate_json(
                        self.rfile.read(int(self.headers["Content-Length"])),
                        strict=True,
                    )
                except ValidationError:
                    self.send_response(422)
                    self.end_headers()
                    return
                op = self.path.removeprefix("/api/v1/operations/current.item.")
                stub.requests.append((op, body))
                if (
                    "?" in self.path
                    or op not in {"create", "get", "update", "set_status"}
                    or self.headers.get("Authorization") != "Bearer synthetic-token"
                ):
                    self.send_response(422)
                    self.end_headers()
                    return
                if stub.unauthorized:
                    self.send_response(401)
                    self.end_headers()
                    return
                if op == stub.refuse_operation or (
                    stub.refuse_source_ref is not None
                    and body.get("source_ref") == stub.refuse_source_ref
                ):
                    self.answer(None, "permission_denied")
                    return
                if op == "create":
                    if stub.before_create:
                        stub.before_create()
                    pair = (body["source_kind"], body["source_ref"])
                    ref = stub.identities.get(pair)
                    if ref is None:
                        ref = f"sample-{len(stub.items) + 1}"
                        stub.identities[pair] = ref
                        stub.items[ref] = {
                            **body,
                            "ref": ref,
                            "revision": 1,
                            "status": "open",
                        }
                        stub.fingerprints[ref] = set()
                        action = "created"
                    else:
                        item = stub.items[ref]
                        action = "grouped"
                        if body["fingerprint"] not in stub.fingerprints[ref]:
                            if item["status"] in {"done", "snoozed"}:
                                action = (
                                    "reopened" if item["status"] == "done" else "woke"
                                )
                                item.update(
                                    {"status": "open", "revision": item["revision"] + 1}
                                )
                    stub.fingerprints[ref].add(body["fingerprint"])
                    if stub.drop_create:
                        stub.drop_create = False
                        self.close_connection = True
                        return
                    self.answer(
                        {
                            "action": action,
                            **(
                                {"ref": ref}
                                if stub.ref_only
                                else {"item": stub.items[ref]}
                            ),
                        }
                    )
                    return
                ref = body["ref"]
                if ref not in stub.items:
                    self.answer(None, "not_found")
                    return
                item = stub.items[ref]
                if op != "get":
                    if stub.before_mutation is not None:
                        stub.before_mutation(op, body)
                    if stub.stale:
                        stub.stale -= 1
                        item["revision"] += 1
                    if item["revision"] != body["revision"]:
                        self.answer(None, "record_stale")
                        return
                    changes = body.copy()
                    del changes["ref"]
                    del changes["revision"]
                    changes.pop("clear", None)
                    item.update(changes)
                    for key in body.get("clear", []):
                        item[key] = None
                    item["revision"] += 1
                self.answer({"item": item})

            def answer(
                self, result: WireResult | None, code: str | None = None
            ) -> None:
                envelope: WireEnvelope = {
                    "state": "succeeded" if code is None else "failed",
                    "operation_id": None if code else stub.operation_id,
                    "result": result,
                }
                if code:
                    envelope["error"] = {
                        "error_code": code,
                        "error_text": "private remote text",
                    }
                raw = json.dumps(envelope).encode()
                self.send_response(
                    stub.stale_http_status
                    if code == "record_stale"
                    else 403
                    if code
                    else 200
                )
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def log_message(self, format: str, *args: object) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(
            target=self.server.serve_forever,
            kwargs={"poll_interval": 0.02},
            daemon=True,
        )
        self.thread.start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def operations(self, *, writes: bool = False) -> list[str]:
        return [op for op, _ in self.requests if not writes or op != "get"]

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
