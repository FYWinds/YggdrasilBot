"""`ygg rpc start|send|close`: host a JSONL stdio process (omp --mode rpc) behind a FIFO and an event file.

The supervisor holds the FIFO open read-write, so it never sees EOF when a writer goes away: every `send` is a
short-lived Execute call. Child stdout lines are appended to events.jsonl (flushed per line), which the bot
reads incrementally over REST; nothing depends on a long-lived connection.
"""

import errno
import fcntl
import json
import os
import subprocess
import threading
from typing import IO

from ygg._common import WORKSPACE, die, emit, exit_record, write_atomic
from ygg._sh import ignore_termination, spawn_supervisor

# Never valid JSON, so `send` (which re-serialises what it is given) cannot produce it.
_CLOSE = b"\x04ygg-close\n"


def start(cli: str, job_dir: str, env_pairs: list[str], cwd: str | None, argv: list[str]) -> int:
    if not argv:
        die("rpc start needs a command after --")
    job_dir = os.path.abspath(job_dir)
    os.makedirs(job_dir, exist_ok=True)
    env = dict(os.environ)
    for pair in env_pairs:
        key, sep, value = pair.partition("=")
        if not sep or not key:
            die(f"--env expects K=V, got {pair!r}")
        env[key] = value
    workdir = os.path.abspath(cwd) if cwd else WORKSPACE
    if not os.path.isdir(workdir):
        die(f"--cwd is not a directory: {workdir}")
    fifo = os.path.join(job_dir, "in")
    for stale in (fifo, os.path.join(job_dir, "exit"), os.path.join(job_dir, "pid")):
        try:
            os.unlink(stale)
        except FileNotFoundError:
            pass
    os.mkfifo(fifo, 0o600)
    proc = spawn_supervisor(cli, ["_supervise", "rpc", job_dir, "--", *argv], cwd=workdir, env=env)
    write_atomic(os.path.join(job_dir, "pid"), str(proc.pid))
    return emit({"pid": proc.pid})


def supervise(job_dir: str, argv: list[str]) -> int:
    ignore_termination()
    fifo = os.open(os.path.join(job_dir, "in"), os.O_RDWR)
    with open(os.path.join(job_dir, "stderr.log"), "ab") as stderr:
        try:
            child = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=stderr)
        except OSError as e:
            stderr.write(f"ygg: cannot start {argv[0]!r}: {e}\n".encode())
            write_atomic(os.path.join(job_dir, "exit"), exit_record(127))
            return 0
    assert child.stdin is not None
    assert child.stdout is not None
    threading.Thread(target=_pump_in, args=(fifo, child.stdin), daemon=True).start()
    with open(os.path.join(job_dir, "events.jsonl"), "ab") as events:
        for line in child.stdout:
            events.write(line if line.endswith(b"\n") else line + b"\n")
            events.flush()
    rc = child.wait()
    write_atomic(os.path.join(job_dir, "exit"), exit_record(rc))
    return 0


def _pump_in(fifo: int, stdin: IO[bytes]) -> None:
    with os.fdopen(fifo, "rb") as source:
        for line in source:
            try:
                if line == _CLOSE:
                    stdin.close()
                    return
                stdin.write(line)
                stdin.flush()
            except (OSError, ValueError):  # the child is gone or its stdin already closed
                return


def _write_fifo(job_dir: str, data: bytes) -> None:
    fifo = os.path.join(os.path.abspath(job_dir), "in")
    try:
        # O_NONBLOCK: fail with ENXIO instead of blocking forever when no supervisor holds the FIFO.
        fd = os.open(fifo, os.O_WRONLY | os.O_NONBLOCK)
    except OSError as e:
        if e.errno in (errno.ENXIO, errno.ENOENT):
            die(f"rpc process is not running ({fifo})", 3)
        raise
    try:
        fcntl.fcntl(fd, fcntl.F_SETFL, fcntl.fcntl(fd, fcntl.F_GETFL) & ~os.O_NONBLOCK)
        view = memoryview(data)
        while view:
            view = view[os.write(fd, view) :]
    finally:
        os.close(fd)


def send(job_dir: str, text: str | None, path: str | None) -> int:
    if (text is None) == (path is None):
        die("rpc send needs exactly one of --json or --file")
    if path is not None:
        with open(path, encoding="utf-8") as f:
            text = f.read()
    assert text is not None
    try:
        message = json.loads(text)
    except ValueError as e:
        die(f"not valid JSON: {e}")
    _write_fifo(job_dir, json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode() + b"\n")
    return emit({"ok": True})


def close(job_dir: str) -> int:
    _write_fifo(job_dir, _CLOSE)
    return emit({"ok": True})
