"""`ygg rpc start|send|close` (the omp stdio host) against a real sandboxd (skipped without one), with a small
Python JSONL echo process standing in for `omp --mode rpc`."""

import json

import pytest

from tests.sandbox.helpers import HAVE_SANDBOXD, NO_SANDBOXD
from tests.sandbox.ygg_cli import YGG, alive, bash, scratch, wait_file, wait_for, ygg
from yggdrasil.sandbox.backend import SandboxdWorkspace

pytestmark = pytest.mark.skipif(not HAVE_SANDBOXD, reason=NO_SANDBOXD)

ECHO = (
    "import json, os, sys\n"
    "print(json.dumps({'type': 'ready', 'cwd': os.getcwd(), 'token': os.environ.get('JOB_TOKEN')}), flush=True)\n"
    "print('diagnostic', file=sys.stderr, flush=True)\n"
    "for line in sys.stdin:\n"
    "    print(json.dumps({'echo': json.loads(line)}, ensure_ascii=False), flush=True)\n"
    "print(json.dumps({'type': 'bye'}), flush=True)\n"
)


async def _events(ws: SandboxdWorkspace, job: str) -> list[dict]:
    return [json.loads(line) for line in (await bash(ws, 'cat "$1/events.jsonl"', job)).splitlines()]


async def _start(ws: SandboxdWorkspace) -> tuple[str, int]:
    job = scratch("rpc")
    r = await ygg(ws, "rpc", "start", job, "--env", "JOB_TOKEN=t-1", "--cwd", "/tmp", "--", "python3", "-u", "-c", ECHO)
    await wait_for(ws, 'grep -q ready "$1/events.jsonl" 2>/dev/null', job)
    return job, r["pid"]


async def test_send_close_and_exit(sandbox_ws: SandboxdWorkspace) -> None:
    job, pid = await _start(sandbox_ws)
    assert (await bash(sandbox_ws, 'cat "$1/pid"', job)).strip() == str(pid)
    assert (await _events(sandbox_ws, job))[0] == {"type": "ready", "cwd": "/tmp", "token": "t-1"}

    assert await ygg(sandbox_ws, "rpc", "send", job, "--json", '{"id": "1", "type": "get_state"}') == {"ok": True}
    await bash(sandbox_ws, 'printf \'{\\n  "type": "prompt",\\n  "message": "你好"\\n}\\n\' > "$1/msg.json"', job)
    assert await ygg(sandbox_ws, "rpc", "send", job, "--file", f"{job}/msg.json") == {"ok": True}
    await wait_for(sandbox_ws, '[ "$(wc -l < "$1/events.jsonl")" -ge 3 ]', job)
    events = await _events(sandbox_ws, job)
    assert events[1] == {"echo": {"id": "1", "type": "get_state"}}
    assert events[2] == {"echo": {"type": "prompt", "message": "你好"}}

    assert await ygg(sandbox_ws, "rpc", "close", job) == {"ok": True}
    record = json.loads(await wait_file(sandbox_ws, f"{job}/exit"))
    assert record["exit"] == 0
    assert (await _events(sandbox_ws, job))[-1] == {"type": "bye"}
    assert (await bash(sandbox_ws, 'cat "$1/stderr.log"', job)) == "diagnostic\n"
    await wait_for(sandbox_ws, '! kill -0 "$1" 2>/dev/null', str(pid))

    gone = await sandbox_ws.exec([YGG, "rpc", "send", job, "--json", "{}"])
    assert gone.exit_code == 3
    assert b"not running" in gone.stderr


async def test_invalid_json_is_rejected(sandbox_ws: SandboxdWorkspace) -> None:
    job, _ = await _start(sandbox_ws)
    bad = await sandbox_ws.exec([YGG, "rpc", "send", job, "--json", "{nope"])
    assert bad.exit_code == 2
    assert len(await _events(sandbox_ws, job)) == 1  # nothing reached the child
    await ygg(sandbox_ws, "proc", "kill", job)


async def test_proc_kill_stops_rpc_job(sandbox_ws: SandboxdWorkspace) -> None:
    job, pid = await _start(sandbox_ws)
    child = int(await bash(sandbox_ws, 'pgrep -P "$1" python3', str(pid)))
    assert await ygg(sandbox_ws, "proc", "kill", job) == {"killed": True}
    record = json.loads(await wait_file(sandbox_ws, f"{job}/exit"))
    assert record["signal"] == 15
    assert not await alive(sandbox_ws, child)
    assert not await alive(sandbox_ws, pid)


async def test_start_failure_is_recorded(sandbox_ws: SandboxdWorkspace) -> None:
    job = scratch("rpc")
    await ygg(sandbox_ws, "rpc", "start", job, "--", "/nonexistent/omp")
    record = json.loads(await wait_file(sandbox_ws, f"{job}/exit"))
    assert record["exit"] == 127
    assert "nonexistent" in await bash(sandbox_ws, 'cat "$1/stderr.log"', job)
