"""A local stand-in for ``/opt/ygg/ygg rpc``: runs a program with a pipe on stdin, appends its stdout lines to
``<dir>/events.jsonl``, its stderr to ``<dir>/stderr.log`` and writes ``<dir>/exit`` when it ends. Implements
``omp.RpcChannel`` so the driver can be tested without a sandbox."""

import asyncio
import json
import sys
import time
from pathlib import Path

from yggdrasil.sandbox import omp
from yggdrasil.sandbox.jobs import parse_exit

FAKE_OMP = Path(__file__).with_name("fake_omp.py")


class LocalRpc:
    def __init__(self, directory: Path) -> None:
        self.dir = directory
        self.dir.mkdir(parents=True, exist_ok=True)
        self.proc: asyncio.subprocess.Process | None = None
        self.sent: list[dict] = []
        self._pumps: list[asyncio.Task[None]] = []
        self._reaper: asyncio.Task[None] | None = None

    async def start(self, env: dict[str, str], argv: list[str] | None = None) -> int:
        self.proc = await asyncio.create_subprocess_exec(
            *(argv or [sys.executable, str(FAKE_OMP)]),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        assert self.proc.stdout is not None
        assert self.proc.stderr is not None
        self._pumps = [
            asyncio.create_task(self._pump(self.proc.stdout, self.dir / "events.jsonl")),
            asyncio.create_task(self._pump(self.proc.stderr, self.dir / "stderr.log")),
        ]
        self._reaper = asyncio.create_task(self._reap())
        return self.proc.pid

    async def _pump(self, stream: asyncio.StreamReader, path: Path) -> None:
        with path.open("ab") as f:
            while line := await stream.readline():
                f.write(line)
                f.flush()

    async def _reap(self) -> None:
        assert self.proc is not None
        code = await self.proc.wait()
        await asyncio.gather(*self._pumps)
        data = {"exit": code if code >= 0 else None, "signal": -code if code < 0 else None, "ended": time.time()}
        tmp = self.dir / "exit.tmp"
        tmp.write_text(json.dumps(data))
        tmp.replace(self.dir / "exit")

    @property
    def running(self) -> bool:
        return self.proc is not None and self.proc.returncode is None

    # --- omp.RpcChannel -------------------------------------------------------------------------------

    async def send(self, frame: dict) -> None:
        if not self.running:
            raise RuntimeError("rpc process is not running")
        assert self.proc is not None
        assert self.proc.stdin is not None
        self.sent.append(frame)
        self.proc.stdin.write((json.dumps(frame, ensure_ascii=False) + "\n").encode())
        await self.proc.stdin.drain()

    async def close_stdin(self) -> None:
        if self.proc is not None and self.proc.stdin is not None and not self.proc.stdin.is_closing():
            self.proc.stdin.close()

    async def kill(self) -> None:
        if self.running:
            assert self.proc is not None
            self.proc.kill()

    async def poll(self, offset: int, limit: int) -> omp.Poll:
        if not self.dir.is_dir():
            return omp.Poll(gone=True, exit=None, data=b"")
        exit_path = self.dir / "exit"
        info = parse_exit(exit_path.read_bytes()) if exit_path.exists() else None
        events = self.dir / "events.jsonl"
        data = b""
        if events.exists():
            with events.open("rb") as f:
                f.seek(offset)
                data = f.read(limit)
        return omp.Poll(gone=False, exit=info, data=data)

    async def stderr_tail(self, limit: int) -> str:
        path = self.dir / "stderr.log"
        return path.read_text("utf-8", "replace")[-limit:] if path.exists() else ""


def fake_env(mode: str = "ok", **extra: str) -> dict[str, str]:
    import os

    return {**os.environ, "FAKE_OMP_MODE": mode, "PYTHONIOENCODING": "utf-8", **extra}
