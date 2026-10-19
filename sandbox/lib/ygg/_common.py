"""Shared pieces of the in-sandbox helpers: JSON output, text cleanup, /proc process walking.

Stdlib only and cheap to import: every `ygg` CLI call is a fresh process started through sandboxd's Execute.
"""

import json
import os
import re
import signal
import sys
import time
from collections.abc import Iterable
from typing import Any, NoReturn

WORKSPACE = "/workspace"
SCRATCH = "/workspace/.ygg"

# CSI / OSC / DCS-style escapes, other two-byte escapes, then any C0 control except tab, newline and CR (NUL
# included: downstream storage rejects it).
_ANSI = re.compile(
    r"\x1b\[[0-?]*[ -/]*[@-~]"
    r"|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)?"
    r"|\x1b[PX^_][^\x1b]*(?:\x1b\\)?"
    r"|\x1b[ -/]*[0-~]"
    r"|[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]"
)
ELLIPSIS = "\n…\n"


def emit(obj: dict[str, Any]) -> int:
    """Write the single JSON result line (UTF-8 regardless of locale) and return exit status 0."""
    sys.stdout.buffer.write(json.dumps(obj, ensure_ascii=False).encode("utf-8", "replace") + b"\n")
    sys.stdout.buffer.flush()
    return 0


def die(message: str, code: int = 2) -> NoReturn:
    sys.stderr.write(f"ygg: {message}\n")
    sys.stderr.flush()
    sys.exit(code)


def clean(text: str) -> str:
    """Strip terminal escapes and render carriage-return overwrites (progress bars) as their final state."""
    text = _ANSI.sub("", text).replace("\r\n", "\n")
    if "\r" not in text:
        return text
    lines = text.split("\n")
    for i, line in enumerate(lines):
        if "\r" in line:
            lines[i] = next((part for part in reversed(line.split("\r")) if part), "")
    return "\n".join(lines)


def cut(text: str, head: int, tail: int) -> tuple[str, bool]:
    """Keep the first ``head`` and last ``tail`` characters, joined by an ellipsis line."""
    if len(text) <= head + tail:
        return text, False
    return text[:head] + ELLIPSIS + (text[-tail:] if tail > 0 else ""), True


def read_output(path: str, head: int, tail: int) -> tuple[str, int, bool]:
    """(cleaned head/tail text, size in bytes, truncated) of an output file, reading only both ends of it."""
    try:
        size = os.path.getsize(path)
    except OSError:
        return "", 0, False
    # A character is at most 4 bytes; the slack absorbs escape sequences removed by clean().
    span_head, span_tail = head * 4 + 4096, tail * 4 + 4096
    with open(path, "rb") as f:
        if size <= span_head + span_tail:
            text, truncated = cut(clean(f.read().decode("utf-8", "replace")), head, tail)
            return text, size, truncated
        first = f.read(span_head)
        f.seek(size - span_tail)
        last = f.read(span_tail)
    first_text = clean(first.decode("utf-8", "replace"))[:head]
    last_text = clean(last.decode("utf-8", "replace")).lstrip("\ufffd")
    return first_text + ELLIPSIS + (last_text[-tail:] if tail > 0 else ""), size, True


def write_atomic(path: str, data: str) -> None:
    tmp = f"{path}.tmp.{os.getpid()}"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(data)
    os.replace(tmp, path)


def read_int(path: str) -> int | None:
    try:
        with open(path, encoding="utf-8") as f:
            return int(f.read().strip())
    except (OSError, ValueError):
        return None


def exit_record(returncode: int | None) -> str:
    """The `exit` file body: exit status or terminating signal, and the end time."""
    code = returncode if returncode is not None and returncode >= 0 else None
    sig = -returncode if returncode is not None and returncode < 0 else None
    return json.dumps({"exit": code, "signal": sig, "ended": time.time()})


# --- processes (/proc only: works under gVisor, where /proc/<pid>/task/*/children is empty) -----------------


def proc_stat(pid: int) -> tuple[str, int, int] | None:
    """(state, ppid, pgid) of a process, or None when it is gone. comm may hold spaces and parentheses, so the
    fields are taken after the last ')'."""
    try:
        with open(f"/proc/{pid}/stat", "rb") as f:
            raw = f.read()
    except OSError:
        return None
    fields = raw[raw.rfind(b")") + 2 :].split()
    if len(fields) < 3:
        return None
    return fields[0].decode(), int(fields[1]), int(fields[2])


def alive(pid: int) -> bool:
    st = proc_stat(pid)
    return st is not None and st[0] not in ("Z", "X")


def cmdline_args(pid: int) -> list[str]:
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            raw = f.read()
    except OSError:
        return []
    return [a.decode("utf-8", "replace") for a in raw.rstrip(b"\0").split(b"\0")] if raw else []


def snapshot() -> dict[int, tuple[int, int]]:
    """pid -> (ppid, pgid) for every live (non-zombie) process."""
    procs: dict[int, tuple[int, int]] = {}
    for name in os.listdir("/proc"):
        if not name.isdigit():
            continue
        st = proc_stat(int(name))
        if st is not None and st[0] not in ("Z", "X"):
            procs[int(name)] = (st[1], st[2])
    return procs


def descendants(roots: Iterable[int], procs: dict[int, tuple[int, int]]) -> set[int]:
    children: dict[int, list[int]] = {}
    for pid, (ppid, _) in procs.items():
        children.setdefault(ppid, []).append(pid)
    found: set[int] = set()
    stack = list(roots)
    while stack:
        for child in children.get(stack.pop(), ()):
            if child not in found:
                found.add(child)
                stack.append(child)
    return found


def _signal_all(pids: Iterable[int], groups: Iterable[int], sig: int) -> None:
    for pgid in groups:
        try:
            os.killpg(pgid, sig)
        except OSError:
            pass
    for pid in pids:
        try:
            os.kill(pid, sig)
        except OSError:
            pass


def kill_tree(pid: int, *, group: bool = True, grace: float = 3.0) -> bool:
    """SIGTERM ``pid``, its descendants and (``group``) its process group; SIGKILL whatever is left after
    ``grace`` seconds. Descendants are re-collected while waiting, before an exiting parent orphans them.
    Returns whether anything was alive to signal."""
    procs = snapshot()
    members = {pid} if pid in procs else set()
    members |= descendants([pid], procs)
    if group:
        members |= {p for p, (_, pgid) in procs.items() if pgid == pid}
    if not members:
        return False
    groups = [pid] if group else []
    _signal_all(members, groups, signal.SIGTERM)
    termed = set(members)
    deadline = time.monotonic() + grace
    while True:
        procs = snapshot()
        members = {p for p in members if p in procs}
        members |= descendants(members, procs)
        if group:
            members |= {p for p, (_, pgid) in procs.items() if pgid == pid}
        if not members or time.monotonic() >= deadline:
            break
        if fresh := members - termed:
            _signal_all(fresh, (), signal.SIGTERM)
            termed |= fresh
        time.sleep(0.05)
    if members:
        _signal_all(members, groups, signal.SIGKILL)
    return True


def group_alive(pgid: int) -> bool:
    return any(g == pgid for _, g in snapshot().values())
