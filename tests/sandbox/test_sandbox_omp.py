"""The omp RPC driver against a fake omp process (tests/sandbox/fake_omp.py) through a local file-backed
channel shaped like ``ygg rpc``: setup, prompt, host tool round trips, chunked frames, cancel, crashes."""

import asyncio
import base64
import json
from pathlib import Path
from typing import Any, cast

import pytest

from tests.sandbox.local_rpc import LocalRpc, fake_env
from yggdrasil.sandbox import SandboxToolError, omp
from yggdrasil.sandbox.backend import UNKNOWN_OUTCOME


class Host:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def __call__(self, name: str, args: dict[str, Any]) -> tuple[str, bool]:
        self.calls.append((name, args))
        if name == "show":
            return "已记下", False
        return "ok", False


def _driver(rpc: LocalRpc, host: Host, state: omp.DriverState | None = None) -> omp.OmpDriver:
    events: list[str] = []

    async def on_event(line: str) -> None:
        events.append(line)

    driver = omp.OmpDriver(
        state or omp.DriverState(),
        rpc,
        host,
        session_dir="/workspace/.ygg/omp/sessions/10001",
        model="claude-test",
        task="做一个网页",
        tools=omp.host_tools(links=False),
        on_event=on_event,
    )
    driver.events = events  # pyright: ignore[reportAttributeAccessIssue]
    return driver


async def _run(driver: omp.OmpDriver, *, seconds: float = 20) -> omp.Finished:
    async with asyncio.timeout(seconds):
        while True:
            if (finished := await driver.step()) is not None:
                return finished
            await asyncio.sleep(0.05)


async def test_full_session_with_host_tool_round_trip(tmp_path: Path) -> None:
    log = tmp_path / "received.jsonl"
    rpc = LocalRpc(tmp_path / "job")
    await rpc.start(fake_env("ok", FAKE_OMP_LOG=str(log)))
    host = Host()
    driver = _driver(rpc, host)
    finished = await _run(driver)

    assert finished.ok, finished.error
    assert finished.text == "完成了。host_1:已记下 | host_2:ok"
    assert host.calls == [
        ("show", {"path": "/workspace/site/shot.png", "caption": "首页截图"}),
        ("progress", {"text": "做到一半"}),
    ]
    received = [json.loads(line) for line in log.read_text("utf-8").splitlines()]
    kinds = [f["type"] for f in received]
    assert kinds[:6] == [
        "negotiate_protocol",
        "set_cache_warming",
        "set_event_filter",
        "set_host_tools",
        "open_session",
        "prompt",
    ]
    assert received[0]["protocolVersion"] == 2  # not "version"
    assert received[1]["mode"] == "off"
    assert received[3]["tools"][0]["loadMode"] == "essential"
    assert [t["name"] for t in received[3]["tools"]] == ["show", "progress"]
    assert received[4] == {
        "id": "setup-session",
        "type": "open_session",
        "sessionDir": "/workspace/.ygg/omp/sessions/10001",
        "provider": "ygg",
        "modelId": "claude-test",
    }
    assert received[5]["message"] == "做一个网页"
    results = [f for f in received if f["type"] == "host_tool_result"]
    assert [r["id"] for r in results] == ["host_1", "host_2"]
    assert results[0]["result"] == {"content": [{"type": "text", "text": "已记下"}]}
    assert "get_last_assistant_text" in kinds
    assert driver.state.phase == "done"
    assert driver.state.handled == ["host_1", "host_2"]
    events = driver.events  # pyright: ignore[reportAttributeAccessIssue]
    assert "开始工作" in events
    assert "bash: ls -la" in events
    assert "bash 出错" in events


async def test_chunked_final_answer_is_reassembled(tmp_path: Path) -> None:
    rpc = LocalRpc(tmp_path / "job")
    await rpc.start(fake_env("ok", FAKE_OMP_BIG="20000", FAKE_OMP_CHUNK="1500"))
    finished = await _run(_driver(rpc, Host()))
    assert finished.ok
    assert finished.text is not None
    assert len(finished.text) == 20000
    raw = (tmp_path / "job" / "events.jsonl").read_text()
    assert '"type":"rpc_chunk"' in raw.replace(" ", "")


async def test_state_survives_a_restart_between_polls(tmp_path: Path) -> None:
    """A new driver built from the saved state (as after a bot restart) continues the same session."""
    rpc = LocalRpc(tmp_path / "job")
    await rpc.start(fake_env("ok"))
    host = Host()
    first = _driver(rpc, host)
    async with asyncio.timeout(10):
        while first.state.phase != "prompting" or not host.calls:
            assert await first.step() is None
            await asyncio.sleep(0.05)
    saved = first.state.to_json()
    second = _driver(rpc, host, omp.DriverState.from_json(saved))
    finished = await _run(second)
    assert finished.ok
    assert [c[0] for c in host.calls] == ["show", "progress"]  # host_1 not run twice


async def test_stop_aborts_a_hanging_run_and_collects_the_answer(tmp_path: Path) -> None:
    rpc = LocalRpc(tmp_path / "job")
    await rpc.start(fake_env("hang"))
    driver = _driver(rpc, Host())
    async with asyncio.timeout(10):
        while driver.state.phase != "prompting":
            assert await driver.step() is None
            await asyncio.sleep(0.05)
    finished = await driver.stop(wait=asyncio.sleep)
    assert not finished.ok
    assert finished.text == "完成了。"
    assert driver.state.status == "aborted"
    sent = [f["type"] for f in rpc.sent]
    assert sent.index("abort") < sent.index("get_last_assistant_text")
    await asyncio.sleep(0.2)
    assert not rpc.running
    exit_info = json.loads((tmp_path / "job" / "exit").read_text())
    assert exit_info["exit"] == 0  # stdin EOF: drained and exited by itself, no kill needed


class FlakyRpc:
    """``LocalRpc`` whose ``send`` fails once for one frame type: ``before`` (never delivered), ``after``
    (delivered, then the connection drops: outcome unknown) or ``cancel`` (the poll is cancelled mid-send)."""

    def __init__(self, rpc: LocalRpc, kind: str, mode: str) -> None:
        self.rpc, self.kind, self.mode, self.failures = rpc, kind, mode, 0

    async def send(self, frame: dict) -> None:
        if frame["type"] == self.kind and not self.failures:
            self.failures += 1
            if self.mode == "after":
                await self.rpc.send(frame)
            if self.mode == "cancel":
                raise asyncio.CancelledError
            raise SandboxToolError(UNKNOWN_OUTCOME)
        await self.rpc.send(frame)

    async def close_stdin(self) -> None:
        await self.rpc.close_stdin()

    async def kill(self) -> None:
        await self.rpc.kill()

    async def poll(self, offset: int, limit: int) -> omp.Poll:
        return await self.rpc.poll(offset, limit)

    async def stderr_tail(self, limit: int) -> str:
        return await self.rpc.stderr_tail(limit)


@pytest.mark.parametrize(
    ("kind", "mode"),
    [
        ("set_host_tools", "before"),
        ("set_host_tools", "after"),
        ("prompt", "before"),
        ("prompt", "after"),
        ("host_tool_result", "before"),
        ("host_tool_result", "after"),
        ("host_tool_result", "cancel"),
        ("get_last_assistant_text", "after"),
    ],
)
async def test_a_failed_send_is_retried_on_the_next_poll(tmp_path: Path, kind: str, mode: str) -> None:
    """A send that fails mid-batch must not lose the frames after it: the next poll handles the same frame
    again (from a fresh driver, as the poller builds one per visit) and re-sends."""
    log = tmp_path / "received.jsonl"
    rpc = LocalRpc(tmp_path / "job")
    await rpc.start(fake_env("ok", FAKE_OMP_LOG=str(log)))
    flaky = FlakyRpc(rpc, kind, mode)
    host = Host()
    state = omp.DriverState()
    errors = 0
    async with asyncio.timeout(20):
        while True:
            driver = _driver(cast(Any, flaky), host, omp.DriverState.from_json(state.to_json()))
            try:
                finished = await driver.step()
            except (SandboxToolError, asyncio.CancelledError):
                errors += 1
                finished = None
            state = driver.state  # what the poller saves, also after a failure
            if finished is not None:
                break
            await asyncio.sleep(0.05)
    assert errors == 1
    assert finished.ok, finished.error
    assert finished.text == "完成了。host_1:已记下 | host_2:ok"
    assert {name for name, _ in host.calls} == {"show", "progress"}
    received = [json.loads(line)["type"] for line in log.read_text("utf-8").splitlines()]
    assert received.count("prompt") == (2 if (kind, mode) == ("prompt", "after") else 1)


async def test_crash_reports_stderr(tmp_path: Path) -> None:
    rpc = LocalRpc(tmp_path / "job")
    await rpc.start(fake_env("crash"))
    finished = await _run(_driver(rpc, Host()))
    assert not finished.ok
    assert "退出码 3" in finished.error
    assert "boom: provider exploded" in finished.error


async def test_setup_failure_closes_the_session(tmp_path: Path) -> None:
    rpc = LocalRpc(tmp_path / "job")
    await rpc.start(fake_env("fail_setup"))
    driver = _driver(rpc, Host())
    finished = await _run(driver)
    assert not finished.ok
    assert "open_session" in finished.error
    assert "Model not found" in finished.error
    assert "prompt" not in [f["type"] for f in rpc.sent]


# --- framing ------------------------------------------------------------------------------------------------


def _chunks(frame: dict, size: int, chunk_id: str = "rpc-1") -> list[bytes]:
    data = json.dumps(frame).encode()
    parts = [data[i : i + size] for i in range(0, len(data), size)]
    return [
        (
            json.dumps(
                {
                    "type": "rpc_chunk",
                    "chunkId": chunk_id,
                    "index": i,
                    "count": len(parts),
                    "byteLength": len(data),
                    "data": base64.b64encode(p).decode(),
                }
            )
            + "\n"
        ).encode()
        for i, p in enumerate(parts)
    ]


def test_decode_leaves_partial_lines_and_sequences_for_the_next_poll() -> None:
    big = {"type": "response", "id": "final", "success": True, "data": {"text": "y" * 300}}
    chunks = _chunks(big, 100)
    head = b'{"type":"ready"}\n' + chunks[0] + chunks[1]
    frames, offset, skip = omp.decode_frames(head + b'{"type":"agen', 0, "")
    assert [d.frame["type"] for d in frames] == ["ready"]
    assert frames[0].end == len(b'{"type":"ready"}\n')
    assert offset == len(b'{"type":"ready"}\n')  # the open sequence is re-read from its first chunk
    assert skip == ""
    rest = b"".join(chunks) + b'{"type":"session_settled"}\n'
    frames, offset2, _ = omp.decode_frames(rest, offset, "")
    assert [d.frame for d in frames] == [big, {"type": "session_settled"}]
    # each frame says where to resume once it is handled: after the big one, the settled frame comes next
    assert frames[0].end == offset + len(b"".join(chunks))
    assert frames[1].end == offset2 == offset + len(rest)


def test_decode_skips_oversized_sequences_across_polls(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(omp, "MAX_FRAME_BYTES", 50)
    chunks = _chunks({"type": "tool_execution_end", "result": "z" * 200}, 60, "rpc-9")
    frames, offset, skip = omp.decode_frames(chunks[0] + chunks[1], 0, "")
    assert frames == []
    assert skip == "rpc-9"
    assert offset == len(chunks[0] + chunks[1])
    tail = b"".join(chunks[2:]) + b'{"type":"agent_start"}\n'
    frames, _, skip = omp.decode_frames(tail, offset, skip)
    assert [d.frame for d in frames] == [{"type": "agent_start"}]
    assert frames[0].skip == ""
    assert skip == ""


def test_models_yaml_and_launch() -> None:
    spec = omp.ModelSpec(
        id="claude-x", name="Claude X", context_window=200000, max_tokens=64000, reasoning=True, vision=True
    )
    doc = json.loads(omp.models_yaml("http://10.0.0.1:8090", [spec]))
    provider = doc["providers"]["ygg"]
    assert provider["api"] == "anthropic-messages"
    assert provider["auth"] == "apiKey"
    assert provider["apiKey"] == omp.TOKEN_ENV
    assert provider["baseUrl"] == "http://10.0.0.1:8090"
    assert provider["models"][0]["input"] == ["text", "image"]
    argv, env = omp.launch(group_id=10001, model="claude-x", rules_path="/r.md", max_time_s=1320, token="t0k")
    assert argv[:3] == [omp.OMP_BIN, "--mode", "rpc"]
    assert argv[argv.index("--model") + 1] == "ygg/claude-x"
    assert argv[argv.index("--session-dir") + 1] == "/workspace/.ygg/omp/sessions/10001"
    assert "task" not in argv[argv.index("--tools") + 1].split(",")
    assert "t0k" not in argv
    assert env[omp.TOKEN_ENV] == "t0k"
    assert env["HOME"] == "/workspace"
    assert env["PI_CONFIG_DIR"] == ".ygg/omp"
