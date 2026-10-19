"""`ygg sh run|start` and `ygg proc kill`: bash scripts written by the bot into a per-call directory."""

import os
import signal
import subprocess
import sys
import time
from typing import Any

from ygg._common import (
    WORKSPACE,
    alive,
    cmdline_args,
    die,
    emit,
    exit_record,
    group_alive,
    kill_tree,
    read_int,
    read_output,
    write_atomic,
)

_BASH = ["bash", "--norc", "--noprofile"]
TERM_GRACE = 3.0


def _script(directory: str) -> str:
    script = os.path.join(directory, "script.sh")
    if not os.path.isfile(script):
        die(f"no script.sh in {directory}")
    return script


def _open_out(directory: str) -> int:
    return os.open(os.path.join(directory, "out"), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)


def _stop_group(proc: subprocess.Popen[bytes]) -> None:
    """SIGTERM the script's process group, give it TERM_GRACE seconds to empty, then SIGKILL the rest."""
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except OSError:
        pass
    deadline = time.monotonic() + TERM_GRACE
    while time.monotonic() < deadline:
        if proc.poll() is not None and not group_alive(proc.pid):
            break
        time.sleep(0.05)
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except OSError:
        pass
    proc.wait()


def run(directory: str, timeout: float, head: int, tail: int) -> int:
    directory = os.path.abspath(directory)
    script = _script(directory)
    out = _open_out(directory)
    started = time.monotonic()
    try:
        proc = subprocess.Popen(
            [*_BASH, script], cwd=WORKSPACE, stdin=subprocess.DEVNULL, stdout=out, stderr=out, start_new_session=True
        )
    finally:
        os.close(out)

    def on_signal(signum: int, _frame: Any) -> None:
        # The Execute call was cancelled: take the script down with us instead of leaving it running.
        _stop_group(proc)
        sys.exit(128 + signum)

    for sig in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
        signal.signal(sig, on_signal)

    timed_out = False
    try:
        proc.wait(timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        _stop_group(proc)
    duration = time.monotonic() - started
    rc = proc.returncode
    out_path = os.path.join(directory, "out")
    output, size, truncated = read_output(out_path, head, tail)
    return emit(
        {
            "exit": rc if rc is not None and rc >= 0 else None,
            "timed_out": timed_out,
            "duration": round(duration, 3),
            "output": output,
            "output_bytes": size,
            "truncated": truncated,
            "out_path": out_path,
        }
    )


def start(cli: str, directory: str) -> int:
    directory = os.path.abspath(directory)
    _script(directory)
    for stale in ("exit", "pid"):
        try:
            os.unlink(os.path.join(directory, stale))
        except FileNotFoundError:
            pass
    proc = spawn_supervisor(cli, ["_supervise", "sh", directory], cwd=WORKSPACE)
    write_atomic(os.path.join(directory, "pid"), str(proc.pid))
    return emit({"pid": proc.pid})


def spawn_supervisor(
    cli: str, args: list[str], *, cwd: str, env: dict[str, str] | None = None
) -> subprocess.Popen[bytes]:
    """A detached supervisor (new session: its pid is its pgid) that outlives this Execute call."""
    return subprocess.Popen(
        [sys.executable, "-IS", cli, *args],
        cwd=cwd,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def ignore_termination() -> None:
    """Supervisors keep running on SIGTERM/SIGHUP/SIGINT: the child (same process group) gets the same signal,
    and the supervisor still has to record how it ended. A group SIGKILL takes both down."""
    for sig in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
        signal.signal(sig, lambda _signum, _frame: None)


def supervise(directory: str) -> int:
    ignore_termination()
    out = _open_out(directory)
    try:
        child = subprocess.Popen(
            [*_BASH, os.path.join(directory, "script.sh")],
            cwd=WORKSPACE,
            stdin=subprocess.DEVNULL,
            stdout=out,
            stderr=out,
        )
    except OSError as e:
        os.write(out, f"ygg: cannot start bash: {e}\n".encode())
        write_atomic(os.path.join(directory, "exit"), exit_record(127))
        return 0
    finally:
        os.close(out)
    rc = child.wait()
    write_atomic(os.path.join(directory, "exit"), exit_record(rc))
    return 0


def owned_pid(directory: str) -> int | None:
    """The supervisor pid of a job dir, if that process is alive and really is the supervisor of this dir (its
    command line names the directory), so a recycled pid is never signalled."""
    directory = os.path.abspath(directory)
    pid = read_int(os.path.join(directory, "pid"))
    if pid is None or pid <= 1 or not alive(pid):
        return None
    args = cmdline_args(pid)
    return pid if "_supervise" in args and directory in args else None


def kill(directory: str) -> int:
    pid = owned_pid(directory)
    return emit({"killed": pid is not None and kill_tree(pid, group=True, grace=TERM_GRACE)})
