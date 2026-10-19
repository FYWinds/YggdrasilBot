"""`ygg sh run|start` and `ygg proc kill` against a real sandboxd (skipped without one)."""

import json

import pytest

from tests.sandbox.helpers import HAVE_SANDBOXD, NO_SANDBOXD
from tests.sandbox.ygg_cli import YGG, alive, bash, put, scratch, wait_file, ygg
from yggdrasil.sandbox.backend import SandboxdWorkspace

pytestmark = pytest.mark.skipif(not HAVE_SANDBOXD, reason=NO_SANDBOXD)


async def _script(ws: SandboxdWorkspace, text: str) -> str:
    d = scratch("sh")
    await put(ws, f"{d}/script.sh", text)
    return d


async def test_run_merges_output_and_reports_exit(sandbox_ws: SandboxdWorkspace) -> None:
    d = await _script(sandbox_ws, 'echo out; echo err >&2; pwd; printf "\\033[31mred\\033[0m\\n"; exit 3\n')
    r = await ygg(sandbox_ws, "sh", "run", d, "--timeout", "10")
    assert r["exit"] == 3
    assert r["timed_out"] is False
    assert r["output"] == "out\nerr\n/workspace\nred\n"
    assert r["output_bytes"] > len(r["output"])  # the escapes count in the file, not in the output
    assert r["truncated"] is False
    assert r["out_path"] == f"{d}/out"


async def test_background_job_does_not_hold_the_call(sandbox_ws: SandboxdWorkspace) -> None:
    d = await _script(sandbox_ws, "sleep 60 &\necho started\n")
    r = await ygg(sandbox_ws, "sh", "run", d, "--timeout", "30", seconds=20)
    assert r["exit"] == 0
    assert r["duration"] < 5
    await bash(sandbox_ws, 'pkill -f "^sleep 60$" || true')


async def test_timeout_kills_the_process_group(sandbox_ws: SandboxdWorkspace) -> None:
    d = await _script(sandbox_ws, 'echo begin\nsleep 300 &\necho $! > "$(dirname "$0")/bg.pid"\nsleep 300\n')
    r = await ygg(sandbox_ws, "sh", "run", d, "--timeout", "1")
    assert r["timed_out"] is True
    assert r["exit"] is None
    assert 1 <= r["duration"] < 6
    assert r["output"] == "begin\n"
    background = int((await bash(sandbox_ws, 'cat "$1/bg.pid"', d)).strip())
    assert not await alive(sandbox_ws, background)


async def test_head_and_tail_truncation(sandbox_ws: SandboxdWorkspace) -> None:
    d = await _script(sandbox_ws, "seq 1 100000\n")
    r = await ygg(sandbox_ws, "sh", "run", d, "--timeout", "30", "--head", "10", "--tail", "12")
    assert r["truncated"] is True
    assert r["output"].startswith("1\n2\n3\n4\n5\n")
    assert r["output"].endswith("\n…\n9999\n100000\n")  # the last 12 characters
    assert "\n…\n" in r["output"]
    assert r["output_bytes"] == len("\n".join(str(i) for i in range(1, 100001))) + 1


async def test_start_writes_pid_and_exit(sandbox_ws: SandboxdWorkspace) -> None:
    d = await _script(sandbox_ws, "echo working\nexit 7\n")
    r = await ygg(sandbox_ws, "sh", "start", d)
    assert (await bash(sandbox_ws, 'cat "$1/pid"', d)).strip() == str(r["pid"])
    record = json.loads(await wait_file(sandbox_ws, f"{d}/exit"))
    assert record["exit"] == 7
    assert record["signal"] is None
    assert record["ended"] > 0
    assert (await bash(sandbox_ws, 'cat "$1/out"', d)) == "working\n"


async def test_proc_kill_stops_job_and_records_signal(sandbox_ws: SandboxdWorkspace) -> None:
    # The grandchild leaves the process group: kill still finds it through the ppid walk.
    d = await _script(sandbox_ws, 'setsid sleep 300 &\necho $! > "$(dirname "$0")/escaped.pid"\nsleep 300\n')
    r = await ygg(sandbox_ws, "sh", "start", d)
    escaped = int((await wait_file(sandbox_ws, f"{d}/escaped.pid")).strip())
    assert await alive(sandbox_ws, r["pid"])
    killed = await ygg(sandbox_ws, "proc", "kill", d)
    assert killed == {"killed": True}
    record = json.loads(await wait_file(sandbox_ws, f"{d}/exit"))
    assert record["exit"] is None
    assert record["signal"] == 15
    assert not await alive(sandbox_ws, r["pid"])
    assert not await alive(sandbox_ws, escaped)
    # Already gone: nothing to kill, and a stale pid file never signals an unrelated process.
    assert await ygg(sandbox_ws, "proc", "kill", d) == {"killed": False}
    await bash(sandbox_ws, 'echo 1 > "$1/pid"', d)
    assert await ygg(sandbox_ws, "proc", "kill", d) == {"killed": False}


async def test_usage_errors_exit_nonzero(sandbox_ws: SandboxdWorkspace) -> None:
    missing = await sandbox_ws.exec([YGG, "sh", "run", scratch("sh"), "--timeout", "1"])
    assert missing.exit_code == 2
    assert b"script.sh" in missing.stderr
    no_timeout = await sandbox_ws.exec([YGG, "sh", "run", "/workspace"])
    assert no_timeout.exit_code == 2
    assert (await sandbox_ws.exec([YGG, "nope", "x"])).exit_code == 2
