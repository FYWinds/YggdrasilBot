"""Workspace helpers preloaded into the group's Python kernel (the `py` tool).

- `show(obj, caption="")`: send a picture (file path, matplotlib figure, PIL image or PNG/JPEG bytes) to the
  group after this cell.
- `publish(port)` / `unpublish(port)`: open / close the public link of a port after this cell.
- `start(cmd, cwd=..., name=None) -> Proc`: a background process kept with the kernel; log in
  /workspace/.ygg/procs/<name>.log.
- `wait_port(port, timeout=30)`: wait until something listens on the port (it must listen on 0.0.0.0).
- `await browser()`, `await shot()`, `await snapshot()`, `await close_browser()`: the persistent Chromium page.

Effects (`show`, `publish`, `unpublish`) are appended to the current cell's effects.jsonl and carried out by the
bot once the cell ends. Imports stay cheap: this module is imported by every kernel at start.
"""

import io
import json
import os
import re
import signal
import subprocess
import sys
import time
from typing import Any

from ygg._browser import Text, browser, close_browser, shot, snapshot

__all__ = [
    "Proc",
    "browser",
    "close_browser",
    "publish",
    "shot",
    "show",
    "snapshot",
    "start",
    "unpublish",
    "wait_port",
]

PROCS_DIR = "/workspace/.ygg/procs"
_CELL_ENV = "YGG_CELL_DIR"
_cell: str | None = None
_last_activity = time.monotonic()
_names: dict[str, int] = {}


# --- cell bookkeeping (called by `ygg py run`) ------------------------------------------------------------


def _begin(cell_dir: str) -> None:
    """Silent preamble of every cell: effects of this cell go to ``cell_dir``. Exported through the environment
    too, so scripts this cell runs (`subprocess`, `!python x.py`) can call `show` / `publish` as well."""
    global _cell
    _cell = cell_dir
    os.environ[_CELL_ENV] = cell_dir
    touch()


def touch() -> None:
    global _last_activity
    _last_activity = time.monotonic()


def last_activity() -> float:
    return _last_activity


def _cell_dir(what: str) -> str:
    cell = _cell or os.environ.get(_CELL_ENV)
    if not cell or not os.path.isdir(cell):
        raise RuntimeError(f"ygg.{what}() 只能在 py 工具执行的代码里调用（sh 里的脚本请改用 py 工具）")
    return cell


def _effect(cell: str, effect: dict[str, Any]) -> None:
    line = json.dumps(effect, ensure_ascii=False) + "\n"
    fd = os.open(os.path.join(cell, "effects.jsonl"), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    try:
        os.write(fd, line.encode())
    finally:
        os.close(fd)


def _note(text: str) -> None:
    sys.stdout.write(text + "\n")


# --- show ----------------------------------------------------------------------------------------------------


def _sniff(data: bytes) -> str | None:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data.startswith(b"\xff\xd8\xff"):
        return "jpg"
    return None


def _png_from_pil(image: Any) -> bytes:
    if image.mode not in ("1", "L", "LA", "P", "RGB", "RGBA", "I", "I;16"):
        image = image.convert("RGBA" if "A" in image.mode else "RGB")
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    return buf.getvalue()


def _image_bytes(obj: Any) -> tuple[bytes, str]:
    if isinstance(obj, (str, os.PathLike)):
        with open(os.path.expanduser(os.fspath(obj)), "rb") as f:
            data = f.read()
    elif isinstance(obj, (bytes, bytearray, memoryview)):
        data = bytes(obj)
    else:
        figure = obj if hasattr(obj, "savefig") else getattr(obj, "figure", None)
        if figure is not None and hasattr(figure, "savefig"):  # matplotlib Figure (or Axes)
            buf = io.BytesIO()
            figure.savefig(buf, format="png", bbox_inches="tight")
            return buf.getvalue(), "png"
        if hasattr(obj, "save") and hasattr(obj, "mode"):  # PIL Image
            return _png_from_pil(obj), "png"
        raise TypeError(f"show() 不支持 {type(obj).__name__}：传文件路径、matplotlib 图、PIL 图片或图片字节")
    kind = _sniff(data)
    if kind is not None:
        return data, kind
    try:  # GIF / WebP / BMP …: convert when Pillow can read it
        from PIL import Image

        with Image.open(io.BytesIO(data)) as image:
            image.load()
            return _png_from_pil(image), "png"
    except Exception:
        raise ValueError("show() 只支持 PNG / JPEG 图片（或 Pillow 能读的格式）") from None


def show(obj: Any, caption: str = "") -> None:
    """Send a picture to the group after this cell: a file path, matplotlib Figure, PIL Image or image bytes."""
    cell = _cell_dir("show")
    data, ext = _image_bytes(obj)
    k = 1 + sum(1 for name in os.listdir(cell) if name.startswith("show-"))
    while True:
        path = os.path.join(cell, f"show-{k}.{ext}")
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
            break
        except FileExistsError:
            k += 1
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    _effect(cell, {"type": "show", "path": path, "caption": str(caption)})


# --- links ---------------------------------------------------------------------------------------------------


def _check_port(port: Any) -> int:
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise ValueError(f"端口必须是 1-65535 的整数，收到 {port!r}")
    return port


def publish(port: int) -> None:
    """Open a public link to ``port`` (the server must listen on 0.0.0.0); the link is in the tool result."""
    port = _check_port(port)
    _effect(_cell_dir("publish"), {"type": "publish", "port": port})
    _note(f"[ygg] 端口 {port} 的公开链接会在这段代码执行完后创建，链接见工具结果。")


def unpublish(port: int) -> None:
    """Remove the public link of ``port`` after this cell."""
    port = _check_port(port)
    _effect(_cell_dir("unpublish"), {"type": "unpublish", "port": port})
    _note(f"[ygg] 端口 {port} 的公开链接会在这段代码执行完后删除。")


# --- background processes ----------------------------------------------------------------------------------


class Proc:
    """A background process started by `start()`: its own session (pid == pgid), output in ``log``."""

    def __init__(self, popen: subprocess.Popen[bytes], name: str, log: str, cmd: str) -> None:
        self._popen = popen
        self.pid = popen.pid
        self.name = name
        self.log = log
        self.cmd = cmd

    def alive(self) -> bool:
        return self._popen.poll() is None

    @property
    def returncode(self) -> int | None:
        return self._popen.poll()

    def tail(self, n: int = 50) -> Text:
        """The last ``n`` lines of the log."""
        from ygg._common import clean

        try:
            with open(self.log, "rb") as f:
                f.seek(max(0, os.path.getsize(self.log) - 256 * 1024))
                text = clean(f.read().decode("utf-8", "replace"))
        except OSError:
            return Text("")
        return Text("\n".join(text.splitlines()[-n:]) if n > 0 else "")

    def stop(self, timeout: float = 3.0) -> int | None:
        """SIGTERM the process group and its descendants, SIGKILL after ``timeout`` seconds."""
        from ygg._common import kill_tree

        if self.alive():
            kill_tree(self.pid, group=True, grace=timeout)
        try:
            return self._popen.wait(timeout=1.0)
        except subprocess.TimeoutExpired:
            os.killpg(self.pid, signal.SIGKILL)
            return self._popen.wait()

    def __repr__(self) -> str:
        rc = self.returncode
        state = "运行中" if rc is None else f"已退出（{rc}）"
        return f"<Proc {self.name} pid={self.pid} {state} log={self.log}>"


def _proc_name(cmd: str, name: str | None) -> str:
    if name is None:
        first = cmd.strip().split(maxsplit=1)[0] if cmd.strip() else "proc"
        name = os.path.basename(first)
    name = re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip(".-") or "proc"
    if name in _names:
        _names[name] += 1
        name = f"{name}-{_names[name]}"
    else:
        _names[name] = 1
    return name


def start(cmd: str, *, cwd: str = "/workspace", name: str | None = None) -> Proc:
    """Run a shell command in the background (kept with the kernel). Output goes to `Proc.log`."""
    name = _proc_name(cmd, name)
    os.makedirs(PROCS_DIR, exist_ok=True)
    log = os.path.join(PROCS_DIR, f"{name}.log")
    with open(log, "wb") as out:
        popen = subprocess.Popen(
            ["bash", "-c", cmd],
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            stdout=out,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    return Proc(popen, name, log, cmd)


def wait_port(port: int, timeout: float = 30) -> dict[str, Any]:
    """Wait until something listens on ``port``; returns the listener (pid, addr, cmdline). Raises when it only
    listens on a loopback address: public links (and other pods) cannot reach it."""
    import ipaddress

    from ygg._port import info as listener

    port = _check_port(port)
    deadline = time.monotonic() + timeout
    while True:
        info = listener(port)
        if info["listening"]:
            addr = info["addr"]
            if not ipaddress.ip_address(addr).is_loopback:
                return info
            raise RuntimeError(
                f"端口 {port} 只在 {addr} 上监听，公开链接访问不到：请让服务监听 0.0.0.0"
                "（例如加 --host 0.0.0.0 或 --bind 0.0.0.0）"
            )
        if time.monotonic() >= deadline:
            raise TimeoutError(f"{timeout:g} 秒内没有程序监听端口 {port}")
        time.sleep(0.2)
