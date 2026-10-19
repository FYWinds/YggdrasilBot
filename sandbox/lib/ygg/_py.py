"""`ygg py run|restart`: the group's persistent IPython kernel.

Speaks the Jupyter wire protocol over pyzmq directly (signed multipart frames) instead of importing
jupyter_client: every cell is a fresh CLI process, and jupyter_client's import alone costs more than a typical
cell. The kernel is started detached (own session) with IPC transport under /workspace/.ygg/kernel/.
"""

import base64
import fcntl
import hashlib
import hmac
import json
import os
import secrets
import signal
import subprocess
import sys
import time
import uuid
from typing import Any

from ygg._common import (
    SCRATCH,
    WORKSPACE,
    alive,
    clean,
    cmdline_args,
    cut,
    die,
    emit,
    kill_tree,
    read_int,
    write_atomic,
)

KERNEL_DIR = f"{SCRATCH}/kernel"
CONNECTION = f"{KERNEL_DIR}/connection.json"
PID_FILE = f"{KERNEL_DIR}/pid"
LOG_FILE = f"{KERNEL_DIR}/kernel.log"
LOCK_FILE = f"{KERNEL_DIR}/lock"
IPC_PREFIX = f"{KERNEL_DIR}/ipc"
KERNEL_MODULE = "ygg._kernel"
LIB_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

START_TIMEOUT = 30.0  # kernel process start -> answering kernel_info
INTERRUPT_GRACE = 5.0  # SIGINT -> execute_reply, else the kernel is killed
MIN_CELL_BUDGET = 1.0  # less left after waiting for the kernel lock (or half the timeout): report busy
IDLE_AFTER_REPLY = 2.0  # execute_reply -> iopub idle (late output)
IMAGE_TYPES = (("image/png", "png"), ("image/jpeg", "jpg"))
_DELIM = b"<IDS|MSG>"


class KernelDied(Exception):
    pass


# --- kernel process -------------------------------------------------------------------------------------


def kernel_state() -> tuple[str, int | None]:
    """("none" | "alive" | "dead", pid of the live kernel)."""
    pid = read_int(PID_FILE)
    if pid is None:
        return ("dead" if os.path.exists(PID_FILE) else "none"), None
    if alive(pid) and any(KERNEL_MODULE in arg for arg in cmdline_args(pid)):
        return "alive", pid
    return "dead", None


def _forget_kernel() -> None:
    for path in (PID_FILE, CONNECTION):
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass


def kill_kernel(pid: int | None) -> bool:
    """Kill the kernel, its descendants and its process group (Chromium and the Playwright driver live there;
    after a crash they are orphans only the group still finds)."""
    killed = kill_tree(pid, group=True, grace=2.0) if pid is not None else False
    _forget_kernel()
    return killed


def start_kernel() -> tuple[int, dict[str, Any]]:
    os.makedirs(KERNEL_DIR, mode=0o700, exist_ok=True)
    _forget_kernel()
    for name in os.listdir(KERNEL_DIR):
        if name.startswith("ipc-"):
            try:
                os.unlink(os.path.join(KERNEL_DIR, name))
            except OSError:
                pass
    conn: dict[str, Any] = {
        "transport": "ipc",
        "ip": IPC_PREFIX,
        "shell_port": 1,
        "iopub_port": 2,
        "stdin_port": 3,
        "control_port": 4,
        "hb_port": 5,
        "key": secrets.token_hex(32),
        "signature_scheme": "hmac-sha256",
        "kernel_name": "ygg",
    }
    fd = os.open(f"{CONNECTION}.tmp", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(conn, f)
    os.replace(f"{CONNECTION}.tmp", CONNECTION)
    env = dict(os.environ)
    env.update(
        MPLBACKEND="module://matplotlib_inline.backend_inline",
        IPYTHONDIR=f"{SCRATCH}/ipython",
        JUPYTER_PLATFORM_DIRS="1",
        PYTHONPATH=os.pathsep.join(p for p in (LIB_DIR, env.get("PYTHONPATH")) if p),
    )
    with open(LOG_FILE, "ab") as log:
        # -P: the workspace (cwd) must not shadow ipykernel or ygg while the kernel boots; the kernel adds it
        # back to sys.path for user imports once started.
        proc = subprocess.Popen(
            [sys.executable, "-P", "-m", KERNEL_MODULE, "-f", CONNECTION],
            cwd=WORKSPACE,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            start_new_session=True,
        )
    write_atomic(PID_FILE, str(proc.pid))
    return proc.pid, conn


def _log_tail(chars: int = 3000) -> str:
    try:
        with open(LOG_FILE, "rb") as f:
            f.seek(max(0, os.path.getsize(LOG_FILE) - chars * 2))
            return clean(f.read().decode("utf-8", "replace"))[-chars:]
    except OSError:
        return ""


# --- wire protocol --------------------------------------------------------------------------------------


class Client:
    def __init__(self, conn: dict[str, Any]) -> None:
        import zmq

        self.zmq = zmq
        self.key = conn["key"].encode()
        self.session = uuid.uuid4().hex
        self.ctx = zmq.Context()
        self.shell = self.ctx.socket(zmq.DEALER)
        self.control = self.ctx.socket(zmq.DEALER)
        self.iopub = self.ctx.socket(zmq.SUB)
        self.iopub.setsockopt(zmq.SUBSCRIBE, b"")
        sockets = ((self.shell, "shell_port"), (self.control, "control_port"), (self.iopub, "iopub_port"))
        for sock, port_key in sockets:
            sock.setsockopt(zmq.LINGER, 0)
            if conn["transport"] == "ipc":
                sock.connect(f"ipc://{conn['ip']}-{conn[port_key]}")
            else:
                sock.connect(f"tcp://{conn['ip']}:{conn[port_key]}")
        self.poller = zmq.Poller()
        for sock, _ in sockets:
            self.poller.register(sock, zmq.POLLIN)

    def close(self) -> None:
        for sock in (self.shell, self.control, self.iopub):
            sock.close(0)
        self.ctx.term()

    def _sign(self, parts: list[bytes]) -> bytes:
        mac = hmac.new(self.key, digestmod=hashlib.sha256)
        for part in parts:
            mac.update(part)
        return mac.hexdigest().encode()

    def send(self, msg_type: str, content: dict[str, Any], *, control: bool = False) -> str:
        msg_id = uuid.uuid4().hex
        header = {
            "msg_id": msg_id,
            "session": self.session,
            "username": "ygg",
            "date": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "msg_type": msg_type,
            "version": "5.3",
        }
        parts = [json.dumps(p).encode() for p in (header, {}, {}, content)]
        (self.control if control else self.shell).send_multipart([_DELIM, self._sign(parts), *parts])
        return msg_id

    def _recv(self, sock: Any) -> dict[str, Any] | None:
        try:
            frames = sock.recv_multipart(self.zmq.NOBLOCK)
        except self.zmq.Again:
            return None
        try:
            i = frames.index(_DELIM)
        except ValueError:
            return {}
        signature, parts = frames[i + 1], frames[i + 2 : i + 6]
        if len(parts) < 4 or not hmac.compare_digest(signature, self._sign(parts)):
            return {}
        header, parent, _metadata, content = (json.loads(p) for p in parts)
        return {"msg_type": header.get("msg_type"), "parent_id": parent.get("msg_id"), "content": content}

    def poll(self, seconds: float) -> list[tuple[str, dict[str, Any]]]:
        """Wait up to ``seconds`` and return every available (channel, message)."""
        ready = dict(self.poller.poll(max(0, int(seconds * 1000))))
        got: list[tuple[str, dict[str, Any]]] = []
        for name, sock in (("iopub", self.iopub), ("shell", self.shell), ("control", self.control)):
            if sock in ready:
                while (msg := self._recv(sock)) is not None:
                    if msg:
                        got.append((name, msg))
        return got

    def handshake(self, pid: int, deadline: float) -> None:
        """kernel_info round trips until our iopub subscription demonstrably receives this session's status
        messages (a SUB socket misses everything published before its subscription reaches the kernel). Uses
        the control channel, which a kernel still busy with an earlier cell answers too."""
        pending: set[str] = set()
        while True:
            pending.add(self.send("kernel_info_request", {}, control=True))
            until = min(deadline, time.monotonic() + 0.25)
            while True:
                for channel, msg in self.poll(until - time.monotonic()):
                    if channel == "iopub" and msg["parent_id"] in pending:
                        return
                if time.monotonic() >= until:
                    break
            if not alive(pid):
                raise KernelDied("kernel exited while starting")
            if time.monotonic() >= deadline:
                raise KernelDied(f"kernel did not answer within {START_TIMEOUT:g}s")


# --- py run ---------------------------------------------------------------------------------------------


class _Output:
    """Stream text in arrival order, bounded: beyond ~4x the kept size only the head and a tail window stay."""

    def __init__(self, head: int, tail: int) -> None:
        self.head, self.tail = head, tail
        self.parts: list[str] = []
        self.size = 0
        self.dropped = False

    def add(self, text: str) -> None:
        self.parts.append(text)
        self.size += len(text)
        if self.size > 4 * (self.head + self.tail) + 65536:
            joined = "".join(self.parts)
            self.parts = [joined[: self.head], joined[-(self.tail + 65536) :]]
            self.size = sum(len(p) for p in self.parts)
            self.dropped = True

    def render(self) -> tuple[str, bool]:
        if self.dropped:
            head, rest = self.parts[0], clean("".join(self.parts[1:]))
            return clean(head)[: self.head] + "\n…\n" + (rest[-self.tail :] if self.tail else ""), True
        return cut(clean("".join(self.parts)), self.head, self.tail)


def _lock(deadline: float) -> int | None:
    os.makedirs(KERNEL_DIR, mode=0o700, exist_ok=True)
    fd = os.open(LOCK_FILE, os.O_RDWR | os.O_CREAT, 0o600)
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return fd
        except BlockingIOError:
            if time.monotonic() >= deadline:
                os.close(fd)
                return None
            time.sleep(0.05)


def run(cell_dir: str, timeout: float, max_images: int, head: int, tail: int) -> int:
    began = time.monotonic()
    cell_dir = os.path.abspath(cell_dir)
    code_path = os.path.join(cell_dir, "code.py")
    try:
        with open(code_path, encoding="utf-8") as f:
            code = f.read()
    except OSError as e:
        die(f"cannot read {code_path}: {e}")

    result: dict[str, Any] = {
        "status": "ok",
        "kernel": {"pid": None, "started": False, "previous": "none"},
        "output": "",
        "output_truncated": False,
        "result": None,
        "error": None,
        "images": [],
        "images_dropped": 0,
        "effects": [],
        "duration": 0.0,
    }

    def finish() -> int:
        result["effects"] = _effects(cell_dir)
        result["duration"] = round(time.monotonic() - began, 3)
        return emit(result)

    def busy() -> int:
        previous, pid = kernel_state()
        result["kernel"].update(pid=pid, previous=previous)
        result["status"] = "error"
        result["error"] = {"ename": "KernelBusy", "evalue": "另一个 py 调用还在运行，稍后再试", "traceback": ""}
        return finish()

    if _lock(began + timeout) is None:
        return busy()
    # Time spent queued behind another call counts against this call's timeout: the bot's Execute deadline is
    # timeout + a fixed grace, so a full fresh timeout here could outlive it and leave the cell unwatched. Too
    # little left (capped at half the timeout, so short uncontended calls still run): report busy instead.
    remaining = timeout - (time.monotonic() - began)
    if remaining < min(MIN_CELL_BUDGET, timeout / 2):
        return busy()

    import site

    site.addsitedir(f"{sys.prefix}/lib/python{sys.version_info[0]}.{sys.version_info[1]}/site-packages")

    previous, pid = kernel_state()
    result["kernel"]["previous"] = previous
    client: Client | None = None
    try:
        if pid is None:
            pid, conn = start_kernel()
            result["kernel"].update(pid=pid, started=True)
            client = Client(conn)
            try:
                client.handshake(pid, time.monotonic() + START_TIMEOUT)
            except KernelDied as e:
                kill_kernel(pid)
                result["status"] = "died"
                result["error"] = {"ename": "KernelStartError", "evalue": str(e), "traceback": _log_tail()}
                return finish()
        else:
            result["kernel"]["pid"] = pid
            with open(CONNECTION, encoding="utf-8") as f:
                client = Client(json.load(f))
            try:
                client.handshake(pid, time.monotonic() + START_TIMEOUT)
            except KernelDied as e:
                kill_kernel(pid)
                result["status"] = "died"
                result["error"] = {"ename": "KernelDied", "evalue": str(e), "traceback": _log_tail()}
                return finish()
        _execute(client, pid, cell_dir, code, remaining, max_images, head, tail, result)
    finally:
        if client is not None:
            client.close()
    return finish()


def _execute(
    client: Client,
    pid: int,
    cell_dir: str,
    code: str,
    timeout: float,
    max_images: int,
    head: int,
    tail: int,
    result: dict[str, Any],
) -> None:
    base = {"user_expressions": {}, "allow_stdin": False}
    client.send(
        "execute_request",
        {**base, "code": f"__import__('ygg')._begin({cell_dir!r})", "silent": True, "store_history": False,
         "stop_on_error": False},
    )  # fmt: skip
    msg_id = client.send(
        "execute_request", {**base, "code": code, "silent": False, "store_history": True, "stop_on_error": True}
    )

    def on_term(_signum: int, _frame: Any) -> None:
        # The Execute call was cancelled: stop the cell rather than leave it running unobserved.
        try:
            os.kill(pid, signal.SIGINT)
        finally:
            sys.exit(143)

    signal.signal(signal.SIGTERM, on_term)
    signal.signal(signal.SIGHUP, on_term)

    output = _Output(head, tail)
    images: list[str] = []
    dropped = 0
    reply: dict[str, Any] | None = None
    idle = False
    interrupted_at: float | None = None
    reply_at = 0.0
    deadline = time.monotonic() + timeout
    died = ""
    while not (reply is not None and idle):
        now = time.monotonic()
        if reply is not None and now >= reply_at + IDLE_AFTER_REPLY:
            break
        if reply is None and interrupted_at is None and now >= deadline:
            os.kill(pid, signal.SIGINT)
            interrupted_at = now
        if reply is None and interrupted_at is not None and now >= interrupted_at + INTERRUPT_GRACE:
            died = "killed"
            break
        if reply is not None:
            wait = reply_at + IDLE_AFTER_REPLY - now
        elif interrupted_at is not None:
            wait = interrupted_at + INTERRUPT_GRACE - now
        else:
            wait = deadline - now
        for channel, msg in client.poll(min(wait, 0.5)):
            if msg["parent_id"] != msg_id:
                continue
            kind, content = msg["msg_type"], msg["content"]
            if channel == "shell":
                if kind == "execute_reply":
                    reply, reply_at = content, time.monotonic()
                continue
            if kind == "stream":
                output.add(content.get("text", ""))
            elif kind in ("execute_result", "display_data", "update_display_data"):
                data = content.get("data", {})
                image = next(((mime, ext) for mime, ext in IMAGE_TYPES if mime in data), None)
                if image is not None:
                    if len(images) < max_images:
                        path = os.path.join(cell_dir, f"out-{len(images) + 1}.{image[1]}")
                        with open(path, "wb") as f:
                            f.write(base64.b64decode(data[image[0]]))
                        images.append(path)
                    else:
                        dropped += 1
                text = data.get("text/plain")
                if kind == "execute_result":
                    result["result"] = cut(clean(text), head, tail)[0] if text is not None else None
                elif image is None and text is not None:
                    output.add(text if text.endswith("\n") else text + "\n")
            elif kind == "error":
                traceback = clean("\n".join(content.get("traceback", [])))
                result["error"] = {
                    "ename": content.get("ename", ""),
                    "evalue": clean(content.get("evalue", "")),
                    "traceback": cut(traceback, head, tail)[0],
                }
            elif kind == "status" and content.get("execution_state") == "idle":
                idle = True
        if reply is None and not died and not alive(pid):
            died = "crashed"
            break

    result["output"], result["output_truncated"] = output.render()
    result["images"], result["images_dropped"] = images, dropped
    if died:
        tail_text = _log_tail()
        kill_kernel(pid)
        result["status"] = "died"
        if result["error"] is None:
            if died == "killed":
                evalue = f"超时后中断 {INTERRUPT_GRACE:g} 秒内没有响应，kernel 已被杀掉，变量都没了"
                result["error"] = {"ename": "KernelKilled", "evalue": evalue, "traceback": ""}
            else:
                evalue = "kernel 进程在执行中退出了（崩溃或被杀），变量都没了"
                result["error"] = {"ename": "KernelDied", "evalue": evalue, "traceback": tail_text}
        return
    assert reply is not None
    status = reply.get("status")
    if status == "ok":
        result["status"] = "ok"
    elif interrupted_at is not None:
        result["status"] = "timeout"
    else:
        result["status"] = "error"
        if result["error"] is None:
            result["error"] = {
                "ename": reply.get("ename", status or "error"),
                "evalue": clean(reply.get("evalue", "")),
                "traceback": cut(clean("\n".join(reply.get("traceback", []))), head, tail)[0],
            }


def _effects(cell_dir: str) -> list[Any]:
    effects: list[Any] = []
    try:
        with open(os.path.join(cell_dir, "effects.jsonl"), encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    effects.append(json.loads(line))
                except ValueError:
                    continue
    except OSError:
        pass
    return effects


def restart() -> int:
    _state, pid = kernel_state()
    return emit({"killed": kill_kernel(pid)})
