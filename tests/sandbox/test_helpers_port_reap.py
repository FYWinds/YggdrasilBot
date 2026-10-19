"""`ygg port` and `reap --keep-pgid/--keep-tree` against a real sandboxd (skipped without one).

reap kills every unprotected process in the sandbox, so these tests leave nothing running that others need."""

import asyncio

import pytest

from tests.sandbox.helpers import HAVE_SANDBOXD, NO_SANDBOXD
from tests.sandbox.ygg_cli import REAP, alive, bash, put, scratch, wait_file, wait_for, ygg
from yggdrasil.sandbox.backend import SandboxdWorkspace

pytestmark = pytest.mark.skipif(not HAVE_SANDBOXD, reason=NO_SANDBOXD)


async def _start(ws: SandboxdWorkspace, script: str) -> tuple[str, int]:
    d = scratch("port")
    await put(ws, f"{d}/script.sh", script)
    r = await ygg(ws, "sh", "start", d)
    return d, r["pid"]


async def test_port_reports_listener(sandbox_ws: SandboxdWorkspace) -> None:
    d, pid = await _start(sandbox_ws, "exec python3 -m http.server 18731 --bind 0.0.0.0\n")
    await wait_for(sandbox_ws, '/opt/ygg/ygg port 18731 | grep -q "\\"listening\\": true"')
    r = await ygg(sandbox_ws, "port", "18731")
    assert r["listening"] is True
    assert r["addr"] == "0.0.0.0"
    assert r["pgid"] == pid  # the supervisor's group
    assert "http.server 18731" in r["cmdline"]
    assert await alive(sandbox_ws, r["pid"])
    await ygg(sandbox_ws, "proc", "kill", d)
    assert (await ygg(sandbox_ws, "port", "18731"))["listening"] is False


async def test_port_reports_loopback_and_absent(sandbox_ws: SandboxdWorkspace) -> None:
    d, _ = await _start(sandbox_ws, "exec python3 -m http.server 18732 --bind 127.0.0.1\n")
    await wait_for(sandbox_ws, '/opt/ygg/ygg port 18732 | grep -q "\\"listening\\": true"')
    assert (await ygg(sandbox_ws, "port", "18732"))["addr"] == "127.0.0.1"
    await ygg(sandbox_ws, "proc", "kill", d)
    absent = await ygg(sandbox_ws, "port", "18733")
    assert absent == {"listening": False, "pid": None, "pgid": None, "addr": None, "cmdline": ""}


async def test_reap_keeps_trees_and_groups(sandbox_ws: SandboxdWorkspace) -> None:
    # tree: supervisor -> bash -> (sleep; setsid sleep in its own session); only --keep-tree spares the latter.
    tree_dir, tree = await _start(sandbox_ws, 'setsid sleep 301 &\necho $! > "$(dirname "$0")/child.pid"\nsleep 302\n')
    group_dir, group = await _start(sandbox_ws, "sleep 303\n")
    stray_dir, stray = await _start(sandbox_ws, "sleep 304\n")
    tree_child = int((await wait_file(sandbox_ws, f"{tree_dir}/child.pid")).strip())
    for supervisor in (tree, group, stray):  # each script's bash is running (and not just its supervisor)
        await wait_for(sandbox_ws, 'pgrep -P "$1" >/dev/null', str(supervisor))

    result = await sandbox_ws.exec([REAP, "--keep-tree", str(tree), "--keep-pgid", str(group)])
    assert result.exit_code == 0, result.stderr
    assert int(result.stdout) >= 3  # the stray supervisor, its bash and its sleep

    assert await alive(sandbox_ws, tree)
    assert await alive(sandbox_ws, tree_child)
    assert await alive(sandbox_ws, group)
    assert not await alive(sandbox_ws, stray)
    for d in (tree_dir, group_dir, stray_dir):
        await ygg(sandbox_ws, "proc", "kill", d)
    assert not await alive(sandbox_ws, tree_child)


async def test_reap_without_keeps_kills_everything(sandbox_ws: SandboxdWorkspace) -> None:
    _, pid = await _start(sandbox_ws, "sleep 305\n")
    result = await sandbox_ws.exec([REAP])
    assert result.exit_code == 0
    assert not await alive(sandbox_ws, pid)
    # sandboxd itself (the reaper's ancestor) is spared: this call works.
    assert (await bash(sandbox_ws, "echo ok")).strip() == "ok"


async def test_reap_spares_execute_calls_in_flight(sandbox_ws: SandboxdWorkspace) -> None:
    # Left behind by a finished `sh run`: reparented to tini, so it is killed.
    d = scratch("port")
    await put(sandbox_ws, f"{d}/script.sh", f"sleep 306 &\necho $! > {d}/orphan.pid\n")
    await ygg(sandbox_ws, "sh", "run", d, "--timeout", "10")
    orphan = int((await wait_file(sandbox_ws, f"{d}/orphan.pid")).strip())
    # Another Execute still running (a job poller's helper): a child of sandboxd, so it is spared.
    in_flight = asyncio.create_task(
        sandbox_ws.exec(["bash", "-c", f"echo $$ > {d}/flight.pid; exec sleep 3"], seconds=30)
    )
    flight = int((await wait_file(sandbox_ws, f"{d}/flight.pid")).strip())

    result = await sandbox_ws.exec([REAP])
    assert result.exit_code == 0, result.stderr
    assert await alive(sandbox_ws, flight)
    assert not await alive(sandbox_ws, orphan)
    assert (await in_flight).exit_code == 0


async def test_reap_rejects_bad_arguments(sandbox_ws: SandboxdWorkspace) -> None:
    for args in (["--keep-tree"], ["--keep-tree", "x"], ["--bogus"]):
        result = await sandbox_ws.exec([REAP, *args])
        assert result.exit_code == 2, args
