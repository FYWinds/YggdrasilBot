"""Calling the in-sandbox helpers (/opt/ygg/ygg, /opt/ygg/reap) the way the bot does: files over REST, commands
through sandboxd Execute. The sandboxd must run the `sandboxd-test` target of sandbox/Dockerfile (or the full
image)."""

import asyncio
import json
import time
import uuid
from typing import Any

from yggdrasil.sandbox.backend import SandboxdWorkspace

YGG = "/opt/ygg/ygg"
REAP = "/opt/ygg/reap"

# A valid 1x1 PNG.
PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
    "1f15c4890000000d49444154789c6360f8cfc0f01f0005000201e5270ab50000000049454e44ae426082"
)


def scratch(kind: str) -> str:
    return f"/workspace/.ygg/test-{kind}/{uuid.uuid4().hex[:12]}"


async def ygg(ws: SandboxdWorkspace, *args: str, seconds: float = 120.0) -> dict[str, Any]:
    result = await ws.exec([YGG, *args], seconds=seconds)
    assert result.exit_code == 0, (args, result.exit_code, result.stderr.decode(errors="replace"))
    lines = result.stdout.decode().splitlines()
    assert len(lines) == 1, result.stdout
    return json.loads(lines[0])


async def bash(ws: SandboxdWorkspace, script: str, *args: str) -> str:
    result = await ws.exec(["bash", "-c", script, "bash", *args])
    assert result.exit_code == 0, (script, result.exit_code, result.stderr.decode(errors="replace"))
    return result.stdout.decode()


async def alive(ws: SandboxdWorkspace, pid: int) -> bool:
    state = await bash(ws, 'cat /proc/"$1"/stat 2>/dev/null | sed "s/.*) //" | cut -d" " -f1', str(pid))
    return state.strip() not in ("", "Z", "X")


async def put(ws: SandboxdWorkspace, path: str, text: str) -> None:
    await ws.write_bytes(path, text.encode())


async def wait_file(ws: SandboxdWorkspace, path: str, within: float = 20.0) -> str:
    deadline = time.monotonic() + within
    while True:
        result = await ws.exec(["cat", path])
        if result.exit_code == 0:
            return result.stdout.decode()
        assert time.monotonic() < deadline, f"{path} did not appear within {within}s"
        await asyncio.sleep(0.2)


async def wait_for(ws: SandboxdWorkspace, script: str, *args: str, within: float = 20.0) -> None:
    """Until ``script`` exits 0."""
    deadline = time.monotonic() + within
    while (await ws.exec(["bash", "-c", script, "bash", *args])).exit_code != 0:
        assert time.monotonic() < deadline, f"condition not met within {within}s: {script}"
        await asyncio.sleep(0.2)
