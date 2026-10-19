"""`ygg py run|restart` (the persistent kernel) and the `ygg` module against a real sandboxd (skipped without one).

Works on the `sandboxd-test` image: no matplotlib or Chromium there, so images come from IPython.display."""

import asyncio
import base64
import itertools
from typing import Any

import pytest

from tests.sandbox.helpers import HAVE_SANDBOXD, NO_SANDBOXD
from tests.sandbox.ygg_cli import PNG, alive, bash, put, scratch, wait_for, ygg
from yggdrasil.sandbox.backend import SandboxdWorkspace

pytestmark = pytest.mark.skipif(not HAVE_SANDBOXD, reason=NO_SANDBOXD)

_cells = itertools.count(1)
_PNG_B64 = base64.b64encode(PNG).decode()


async def run(ws: SandboxdWorkspace, code: str, limit: float = 30, *extra: str) -> dict[str, Any]:
    d = f"{scratch('py')}-{next(_cells)}"
    await put(ws, f"{d}/code.py", code)
    return await ygg(ws, "py", "run", d, "--timeout", str(limit), *extra, seconds=limit + 60)


@pytest.fixture
async def fresh(sandbox_ws: SandboxdWorkspace) -> SandboxdWorkspace:
    """No kernel before the test (state from other tests never leaks in)."""
    await ygg(sandbox_ws, "py", "restart")
    return sandbox_ws


async def test_state_persists_and_kernel_is_reported(fresh: SandboxdWorkspace) -> None:
    first = await run(fresh, "x = 41\nprint('hello')\nx + 1")
    assert first["status"] == "ok"
    assert first["kernel"]["started"] is True
    assert first["kernel"]["previous"] == "none"
    assert first["output"] == "hello\n"
    assert first["result"] == "42"
    assert first["error"] is None
    second = await run(fresh, "x")
    assert second["kernel"] == {"pid": first["kernel"]["pid"], "started": False, "previous": "alive"}
    assert second["result"] == "41"
    assert (await bash(fresh, "cat /workspace/.ygg/kernel/pid")).strip() == str(first["kernel"]["pid"])


async def test_preloaded_names_and_top_level_await(fresh: SandboxdWorkspace) -> None:
    r = await run(
        fresh,
        "import asyncio\nawait asyncio.sleep(0.01)\n"
        "print(all(callable(f) for f in (show, publish, unpublish, start, wait_port, browser, shot, snapshot)))\n"
        "ygg.__name__",
    )
    assert r["status"] == "ok", r
    assert r["output"] == "True\n"
    assert r["result"] == "'ygg'"


async def test_error_has_clean_traceback(fresh: SandboxdWorkspace) -> None:
    r = await run(fresh, "print('before')\ndef f():\n    return 1 / 0\nf()")
    assert r["status"] == "error"
    assert r["output"] == "before\n"
    error = r["error"]
    assert error["ename"] == "ZeroDivisionError"
    assert error["evalue"] == "division by zero"
    assert "return 1 / 0" in error["traceback"]
    assert "\x1b" not in error["traceback"]


async def test_stream_order_and_display_text(fresh: SandboxdWorkspace) -> None:
    code = (
        "import sys\nfrom IPython.display import display\n"
        "print('a', flush=True)\nsys.stderr.write('b\\n'); sys.stderr.flush()\ndisplay('shown')\n"
        "print('\\x1b[1mc\\x1b[0m')"
    )
    r = await run(fresh, code)
    assert r["output"] == "a\nb\n'shown'\nc\n"


async def test_images_are_saved_and_capped(fresh: SandboxdWorkspace) -> None:
    code = (
        "import base64\nfrom IPython.display import Image, display\n"
        f"data = base64.b64decode('{_PNG_B64}')\n"
        "for _ in range(3):\n    display(Image(data=data, format='png'))\n"
    )
    r = await run(fresh, code, 30, "--max-images", "2")
    assert r["status"] == "ok"
    assert len(r["images"]) == 2
    assert r["images_dropped"] == 1
    assert all(path.endswith(".png") and "/out-" in path for path in r["images"])
    assert base64.b64decode(await bash(fresh, 'base64 -w0 "$1"', r["images"][0])) == PNG


async def test_output_is_truncated(fresh: SandboxdWorkspace) -> None:
    r = await run(fresh, "for i in range(20000):\n    print(i)", 30, "--head", "20", "--tail", "30")
    assert r["output_truncated"] is True
    assert r["output"].startswith("0\n1\n2\n")
    assert r["output"].endswith("19998\n19999\n")
    assert len(r["output"]) <= 20 + 30 + 3


async def test_effects_show_publish(fresh: SandboxdWorkspace) -> None:
    code = (
        f"import base64\nshow(base64.b64decode('{_PNG_B64}'), caption='图')\n"
        "publish(8000)\nunpublish(8001)\n"
        "import subprocess, sys\n"  # scripts the cell runs inherit the cell
        f"subprocess.run([sys.executable, '-c', 'import ygg, base64; ygg.show(base64.b64decode(\"{_PNG_B64}\"))'], "
        "check=True)\n"
    )
    r = await run(fresh, code)
    assert r["status"] == "ok", r
    effects = r["effects"]
    assert [e["type"] for e in effects] == ["show", "publish", "unpublish", "show"]
    assert effects[0]["caption"] == "图"
    assert effects[0]["path"].endswith("/show-1.png")
    assert effects[3]["path"].endswith("/show-2.png")
    assert effects[1] == {"type": "publish", "port": 8000}
    assert effects[2] == {"type": "unpublish", "port": 8001}
    assert "8000" in r["output"]
    assert base64.b64decode(await bash(fresh, 'base64 -w0 "$1"', effects[0]["path"])) == PNG

    bad = await run(fresh, "publish(70000)")
    assert bad["status"] == "error"
    assert bad["effects"] == []


async def test_effects_need_a_cell(sandbox_ws: SandboxdWorkspace) -> None:
    result = await sandbox_ws.exec(["python3", "-c", "import ygg; ygg.publish(8000)"])
    assert result.exit_code != 0
    assert "py" in result.stderr.decode()


async def test_timeout_interrupts_and_keeps_state(fresh: SandboxdWorkspace) -> None:
    await run(fresh, "kept = 'yes'")
    r = await run(fresh, "import time\nprint('start')\ntime.sleep(60)", 1)
    assert r["status"] == "timeout"
    assert r["output"] == "start\n"
    assert r["error"]["ename"] == "KeyboardInterrupt"
    assert r["duration"] < 6
    after = await run(fresh, "kept")
    assert after["kernel"]["previous"] == "alive"
    assert after["result"] == "'yes'"


async def _hold_kernel(ws: SandboxdWorkspace, seconds: float) -> "asyncio.Task[dict[str, Any]]":
    """Start a cell that keeps the kernel (and its lock) busy for ``seconds``; returns once it is running."""
    marker = f"{scratch('py')}-held"
    holder = asyncio.create_task(
        run(ws, f"import time\nopen({marker!r}, 'w').close()\ntime.sleep({seconds})", seconds + 10)
    )
    await wait_for(ws, 'test -e "$1"', marker)
    return holder


async def test_queued_call_only_gets_the_rest_of_its_timeout(fresh: SandboxdWorkspace) -> None:
    await run(fresh, "1")  # kernel up, so the lock wait is the only delay
    holder = await _hold_kernel(fresh, 2)
    queued = await run(fresh, "import time\ntime.sleep(60)", 4)
    assert (await holder)["status"] == "ok"
    assert queued["status"] == "timeout"
    # ~2 s queued + ~2 s running: a fresh 4 s budget after the wait would take ~6 s.
    assert queued["duration"] < 5.2, queued["duration"]

    holder = await _hold_kernel(fresh, 2)
    starved = await run(fresh, "never = 1", 2.5)  # under a second left once the lock is free
    await holder
    assert starved["status"] == "error"
    assert starved["error"]["ename"] == "KernelBusy"
    assert (await run(fresh, "'never' in dir()"))["result"] == "False"


async def test_unresponsive_kernel_is_killed(fresh: SandboxdWorkspace) -> None:
    first = await run(fresh, "kept = 1")
    r = await run(fresh, "import signal, time\nsignal.signal(signal.SIGINT, signal.SIG_IGN)\ntime.sleep(60)", 1)
    assert r["status"] == "died"
    assert r["error"]["ename"] == "KernelKilled"
    assert not await alive(fresh, first["kernel"]["pid"])
    after = await run(fresh, "'kept' in dir()")
    assert after["kernel"]["started"] is True
    assert after["result"] == "False"


async def test_crash_and_vanished_kernel(fresh: SandboxdWorkspace) -> None:
    crashed = await run(fresh, "import os\nos._exit(3)")
    assert crashed["status"] == "died"
    assert crashed["error"]["ename"] == "KernelDied"
    # The crash was reported, so the next call simply starts fresh.
    restarted = await run(fresh, "1")
    assert restarted["kernel"]["started"] is True
    assert restarted["kernel"]["previous"] == "none"
    # A kernel that vanished between calls (idle exit, reap, kill) is reported as dead.
    await bash(fresh, 'kill -9 "$1"', str(restarted["kernel"]["pid"]))
    await wait_for(fresh, '! kill -0 "$1" 2>/dev/null', str(restarted["kernel"]["pid"]))
    after = await run(fresh, "2")
    assert after["kernel"]["started"] is True
    assert after["kernel"]["previous"] == "dead"
    assert after["result"] == "2"


async def test_restart_kills_kernel_tree(fresh: SandboxdWorkspace) -> None:
    r = await run(fresh, "p = start('sleep 300', name='sleeper')\np.pid")
    child = int(r["result"])
    assert await alive(fresh, child)
    assert await ygg(fresh, "py", "restart") == {"killed": True}
    assert not await alive(fresh, r["kernel"]["pid"])
    assert not await alive(fresh, child)
    assert await ygg(fresh, "py", "restart") == {"killed": False}


async def test_start_wait_port_and_port(fresh: SandboxdWorkspace) -> None:
    r = await run(
        fresh,
        "web = start('exec python3 -m http.server 18741 --bind 0.0.0.0', name='web')\n"
        "info = wait_port(18741, timeout=20)\nprint(info['addr'], info['pid'] == web.pid)\nweb",
    )
    assert r["status"] == "ok", r
    assert r["output"] == "0.0.0.0 True\n"
    assert "<Proc web pid=" in r["result"]
    listener = await ygg(fresh, "port", "18741")
    assert listener["pgid"] == listener["pid"]  # start() gives every process its own session
    assert "/workspace/.ygg/procs/web.log" in (await run(fresh, "web.log"))["result"]

    loopback = await run(
        fresh, "lo = start('exec python3 -m http.server 18742 --bind 127.0.0.1')\nwait_port(18742, timeout=20)"
    )
    assert loopback["status"] == "error"
    assert "0.0.0.0" in loopback["error"]["evalue"]
    missing = await run(fresh, "wait_port(18743, timeout=0.5)")
    assert missing["error"]["ename"] == "TimeoutError"
    stopped = await run(fresh, "web.stop(), lo.stop(), web.alive()")
    assert stopped["result"].endswith("False)")
