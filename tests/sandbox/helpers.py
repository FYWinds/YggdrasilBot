"""Test doubles for the sandbox tests: a fake Kubernetes API, an in-memory sandboxd, and the local sandboxd settings
the image tests (helpers, conformance) use."""

import asyncio
import collections
import json
import os
import posixpath
from collections.abc import Sequence
from typing import Any

import httpx
import pytest
from pydantic_ai.workspaces import CommandResult, FileEntry

from yggdrasil.sandbox.backend import SandboxdWorkspace, _Exec
from yggdrasil.sandbox.manager import _TREE_SCRIPT

SANDBOXD_REST = os.environ.get("YGG_TEST_SANDBOXD_REST")
SANDBOXD_GRPC = os.environ.get("YGG_TEST_SANDBOXD_GRPC")
SANDBOXD_TOKEN = os.environ.get("YGG_TEST_SANDBOXD_TOKEN")
HAVE_SANDBOXD = bool(SANDBOXD_REST and SANDBOXD_GRPC and SANDBOXD_TOKEN)
NO_SANDBOXD = "set YGG_TEST_SANDBOXD_REST / YGG_TEST_SANDBOXD_GRPC / YGG_TEST_SANDBOXD_TOKEN to run against a sandboxd"


class FakeKube:
    """Just enough of the API server for the manager: objects keyed by path, claims Ready immediately.

    ``claim_gate`` (when set) holds every claim GET until it is set, and ``claim_get_entered`` fires when one
    is waiting; ``fail_patch`` makes PATCH answer 500; ``patches`` counts PATCH calls."""

    def __init__(self) -> None:
        self.objects: dict[str, dict[str, Any]] = {}
        self.claim_gate: asyncio.Event | None = None
        self.claim_get_entered = asyncio.Event()
        self.fail_patch = False
        self.patches = 0

    async def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        body: dict[str, Any] = json.loads(request.content) if request.content else {}
        if request.method == "POST":
            name = body["metadata"]["name"]
            obj = {**body, "metadata": {**body["metadata"], "uid": f"uid-{name}"}}
            if path.endswith("/sandboxclaims"):
                obj["status"] = {
                    "conditions": [{"type": "Ready", "status": "True"}],
                    "sandbox": {"name": name, "podIPs": ["127.0.0.1"]},
                }
                sandbox_path = path.replace("extensions.agents.x-k8s.io/v1beta1", "agents.x-k8s.io/v1beta1").replace(
                    "sandboxclaims", "sandboxes"
                )
                self.objects[f"{sandbox_path}/{name}"] = {"metadata": {"name": name, "uid": f"uid-sb-{name}"}}
            self.objects[f"{path}/{name}"] = obj
            return httpx.Response(201, json=obj)
        if request.method == "GET" and path.endswith(("/sandboxclaims", "/services", "/ingressroutes")):
            wanted = dict(x.split("=", 1) for x in request.url.params.get("labelSelector", "").split(",") if x)
            items = [
                v
                for k, v in self.objects.items()
                if k.rsplit("/", 1)[0] == path and wanted.items() <= (v.get("metadata", {}).get("labels") or {}).items()
            ]
            return httpx.Response(200, json={"items": items})
        if request.method == "GET" and "/sandboxclaims/" in path and self.claim_gate is not None:
            self.claim_get_entered.set()
            await self.claim_gate.wait()
        obj = self.objects.get(path)
        if obj is None:
            return httpx.Response(404, json={"message": "not found"})
        if request.method == "GET":
            return httpx.Response(200, json=obj)
        if request.method == "PATCH":
            self.patches += 1
            if self.fail_patch:
                return httpx.Response(500, json={"message": "boom"})
            obj["spec"]["lifecycle"].update(body["spec"]["lifecycle"])
            return httpx.Response(200, json=obj)
        if request.method == "DELETE":
            del self.objects[path]
            if "/sandboxclaims/" in path:  # the controller cascades to the Sandbox and its links
                name = path.rsplit("/", 1)[1]
                for key in [k for k in self.objects if k.endswith(f"/sandboxes/{name}")]:
                    del self.objects[key]
            return httpx.Response(200, json={})
        return httpx.Response(405)

    def claims(self) -> list[dict[str, Any]]:
        return [v for k, v in self.objects.items() if "/sandboxclaims/" in k]

    def claim_names(self) -> list[str]:
        return sorted(c["metadata"]["name"] for c in self.claims())

    def claim(self, name: str) -> dict[str, Any]:
        return next(c for c in self.claims() if c["metadata"]["name"] == name)


class MemorySandboxd:
    """Stands in for the sandboxd behind every claim: files live in memory per workspace (keyed by claim), helper
    commands are recorded and succeed. Only ``SandboxdWorkspace``'s gRPC / REST I/O is replaced; attaching,
    renewal, the command budget and attach notices run for real."""

    def __init__(self) -> None:
        self.files: dict[str, dict[str, bytes]] = collections.defaultdict(dict)
        self.commands: list[list[str]] = []

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        pods = self

        async def files(ws: SandboxdWorkspace) -> dict[str, bytes]:
            await ws._ensure()  # pyright: ignore[reportPrivateUsage]
            await ws._touch()  # pyright: ignore[reportPrivateUsage]
            assert ws.ref is not None
            return pods.files[ws.ref.id]

        async def exec_(ws: SandboxdWorkspace, argv: Sequence[str], **_: Any) -> _Exec:
            fs = await files(ws)
            pods.commands.append(list(argv))
            if list(argv[:3]) == ["/bin/sh", "-c", _TREE_SCRIPT]:
                root = argv[4].rstrip("/") + "/"
                rows = [f"f\t{len(v)}\t0\t{k.removeprefix(root)}" for k, v in sorted(fs.items()) if k.startswith(root)]
                return _Exec(0, "\n".join(rows).encode(), b"")
            if argv[-1].endswith("/NOTES.md"):
                return _Exec(0, fs[argv[-1]], b"") if argv[-1] in fs else _Exec(1, b"", b"")
            return _Exec(0, b"0\n", b"")

        async def start(ws: SandboxdWorkspace, conn: Any, argv: list[str], env: Any, seconds: Any, note: Any):
            pods.commands.append(argv)
            return CommandResult(exit_code=0, stdout="", stderr="")

        async def write_bytes(ws: SandboxdWorkspace, path: str, data: bytes) -> None:
            (await files(ws))[path] = bytes(data)

        async def read_bytes(ws: SandboxdWorkspace, path: str) -> bytes:
            fs = await files(ws)
            if path not in fs:
                raise FileNotFoundError(path)
            return fs[path]

        async def stat(ws: SandboxdWorkspace, path: str) -> FileEntry:
            fs = await files(ws)
            name = posixpath.basename(path)
            if path in fs:
                return FileEntry(name=name, path=path, is_dir=False, size=len(fs[path]))
            if any(k.startswith(path.rstrip("/") + "/") for k in fs):
                return FileEntry(name=name, path=path, is_dir=True, size=None)
            raise FileNotFoundError(path)

        monkeypatch.setattr(SandboxdWorkspace, "exec", exec_)
        monkeypatch.setattr(SandboxdWorkspace, "_start", start)
        monkeypatch.setattr(SandboxdWorkspace, "write_bytes", write_bytes)
        monkeypatch.setattr(SandboxdWorkspace, "read_bytes", read_bytes)
        monkeypatch.setattr(SandboxdWorkspace, "stat", stat)
