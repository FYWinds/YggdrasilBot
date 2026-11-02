"""Background job files and processes inside a workspace, through ``/opt/ygg/ygg`` and small POSIX shell helpers:
what the runner (``yggdrasil.sandbox.jobs``) starts, polls, stops and reads jobs with."""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from yggdrasil.sandbox import omp
from yggdrasil.sandbox.errors import SandboxError
from yggdrasil.sandbox.state import random_id

YGG = "/opt/ygg/ygg"
_SEND_INLINE = 32 * 1024  # frames larger than this go through a file (``ygg rpc send --file``)

_ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[@-Z\\-_]")

# $1 job dir, $2 events file name ("" for none), $3 byte offset, $4 max bytes. Exit 3: the dir is gone.
# The exit file is read first, so events read after it are complete when the job has ended. A supervisor that
# is gone without an exit file was SIGKILLed (no chance to write it): reported as signal 9.
_POLL_SCRIPT = (
    '[ -d "$1" ] || exit 3; '
    'if [ -f "$1/exit" ]; then cat -- "$1/exit"; '
    'else p=$(cat -- "$1/pid" 2>/dev/null); '
    'if [ -n "$p" ] && ! kill -0 "$p" 2>/dev/null; then '
    'if [ -f "$1/exit" ]; then cat -- "$1/exit"; else printf \'{"exit":null,"signal":9}\'; fi; fi; fi; '
    'printf "\\0"; '
    '[ -n "$2" ] || exit 0; '
    'tail -c +"$(($3 + 1))" -- "$1/$2" 2>/dev/null | head -c "$4"; exit 0'
)
# $1 file, $2 head bytes, $3 tail bytes: "<size>\n" + head + tail (the tail never overlaps the head).
_OUTPUT_SCRIPT = (
    '[ -f "$1" ] || { printf "0\\n"; exit 0; }; s=$(stat -c %s -- "$1"); printf "%s\\n" "$s"; '
    'head -c "$2" -- "$1"; r=$((s - $2)); [ "$r" -gt "$3" ] && r=$3; '
    '[ "$r" -gt 0 ] && tail -c "$r" -- "$1"; exit 0'
)
_SIZE_SCRIPT = '[ -f "$1" ] || exit 3; stat -L -c %s -- "$1"'


def clean_output(raw: bytes) -> str:
    """Terminal output as plain text: UTF-8 with replacement, ANSI escapes stripped, ``\\r`` progress bars
    collapsed to their last state."""
    text = _ANSI.sub("", raw.decode("utf-8", errors="replace"))
    return "\n".join(line.rsplit("\r", 1)[-1] if "\r" in line else line for line in text.split("\n"))


class Exec(Protocol):
    exit_code: int
    stdout: bytes
    stderr: bytes


class JobBackend(Protocol):
    """The part of ``SandboxdWorkspace`` jobs use."""

    async def exec(self, argv: Sequence[str], *, seconds: float = ..., retry: bool = ..., once: bool = ...) -> Exec: ...
    async def write_bytes(self, path: str, data: bytes) -> None: ...
    async def aclose(self) -> None: ...


@dataclass(frozen=True)
class Output:
    text: str
    size: int
    truncated: bool


class JobOps:
    """Job files and processes in one workspace, through ``/opt/ygg/ygg`` and small shell helpers."""

    def __init__(self, backend: JobBackend) -> None:
        self._b = backend

    async def _ygg(self, *args: str, seconds: float = 30, once: bool = False) -> dict[str, Any]:
        """Run ``/opt/ygg/ygg``; ``once`` for commands that must not run twice (they start a process or write
        into omp's stdin): a connection lost after sending raises instead of re-running them."""
        result = await self._b.exec([YGG, *args], seconds=seconds, once=once)
        out = result.stdout.decode("utf-8", errors="replace").strip()
        if result.exit_code != 0 or not out:
            err = result.stderr.decode("utf-8", errors="replace").strip() or out or f"exit {result.exit_code}"
            raise SandboxError(f"ygg {' '.join(args[:2])} 失败：{err[:500]}")
        try:
            return json.loads(out.splitlines()[-1])
        except ValueError as e:
            raise SandboxError(f"ygg {' '.join(args[:2])} 的输出看不懂：{out[:200]}") from e

    async def write(self, path: str, data: bytes) -> None:
        await self._b.write_bytes(path, data)

    async def start_sh(self, directory: str) -> int:
        return int((await self._ygg("sh", "start", directory, once=True))["pid"])

    async def start_rpc(self, directory: str, argv: list[str], env: dict[str, str], *, cwd: str) -> int:
        args = ["rpc", "start", directory, "--cwd", cwd]
        for key, value in env.items():
            args += ["--env", f"{key}={value}"]
        return int((await self._ygg(*args, "--", *argv, once=True))["pid"])

    async def kill(self, directory: str) -> None:
        await self._ygg("proc", "kill", directory, seconds=20)

    async def send(self, directory: str, frame: dict[str, Any]) -> None:
        text = json.dumps(frame, ensure_ascii=False, separators=(",", ":"))
        if len(text.encode()) <= _SEND_INLINE:
            await self._ygg("rpc", "send", directory, "--json", text, once=True)
            return
        path = f"{directory}/send-{random_id(6)}.json"
        await self.write(path, text.encode())
        await self._ygg("rpc", "send", directory, "--file", path, once=True)

    async def close_stdin(self, directory: str) -> None:
        await self._ygg("rpc", "close", directory)

    async def poll(self, directory: str, events: str = "", offset: int = 0, limit: int = 0) -> omp.Poll:
        result = await self._b.exec(
            ["/bin/sh", "-c", _POLL_SCRIPT, "sh", directory, events, str(offset), str(limit)], seconds=30
        )
        if result.exit_code == 3:
            return omp.Poll(gone=True, exit=None, data=b"")
        if result.exit_code != 0:
            raise SandboxError(f"读任务状态失败：{result.stderr.decode('utf-8', 'replace')[:300]}")
        raw_exit, _, data = result.stdout.partition(b"\0")
        return omp.Poll(gone=False, exit=parse_exit(raw_exit), data=data)

    async def output(self, path: str, *, head: int, tail: int) -> Output:
        result = await self._b.exec(["/bin/sh", "-c", _OUTPUT_SCRIPT, "sh", path, str(head), str(tail)], seconds=30)
        first, _, rest = result.stdout.partition(b"\n")
        size = int(first) if first.strip().isdigit() else 0
        head_bytes = rest[: min(head, size)]
        tail_bytes = rest[min(head, size) :]
        truncated = size > head + tail
        text = clean_output(head_bytes)
        if tail_bytes:
            gap = size - len(head_bytes) - len(tail_bytes)
            text += f"\n…（中间省略 {gap} 字节）…\n" if truncated else ""
            text += clean_output(tail_bytes)
        return Output(text=text, size=size, truncated=truncated)

    async def tail_text(self, path: str, chars: int) -> str:
        return (await self.output(path, head=0, tail=chars * 3)).text[-chars:]

    async def file_size(self, path: str) -> int | None:
        result = await self._b.exec(["/bin/sh", "-c", _SIZE_SCRIPT, "sh", path], seconds=20)
        out = result.stdout.decode().strip()
        return int(out) if result.exit_code == 0 and out.isdigit() else None


def parse_exit(raw: bytes) -> omp.ExitInfo | None:
    raw = raw.strip()
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        return omp.ExitInfo(exit=None, signal=None)
    code, sig = data.get("exit"), data.get("signal")
    return omp.ExitInfo(exit=code if isinstance(code, int) else None, signal=sig if isinstance(sig, int) else None)


class JobChannel:
    """``omp.RpcChannel`` over one job directory."""

    def __init__(self, ops: JobOps, directory: str) -> None:
        self._ops = ops
        self._dir = directory

    async def send(self, frame: dict[str, Any]) -> None:
        await self._ops.send(self._dir, frame)

    async def close_stdin(self) -> None:
        await self._ops.close_stdin(self._dir)

    async def kill(self) -> None:
        await self._ops.kill(self._dir)

    async def poll(self, offset: int, limit: int) -> omp.Poll:
        return await self._ops.poll(self._dir, "events.jsonl", offset, limit)

    async def stderr_tail(self, limit: int) -> str:
        return await self._ops.tail_text(f"{self._dir}/stderr.log", limit)
