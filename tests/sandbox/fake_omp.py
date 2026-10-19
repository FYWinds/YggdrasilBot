"""A tiny stand-in for ``omp --mode rpc`` speaking the frames the bot's driver uses (see omp's docs/rpc.md).

Behaviour is picked with ``FAKE_OMP_MODE``:

- ``ok``: after ``prompt`` it calls the host tools ``show`` and ``progress`` (waiting for each result), then
  completes with ``sessionSettled: false`` followed by ``session_settled``; the final text quotes both results.
- ``hang``: after ``prompt`` it works forever until ``abort``.
- ``crash``: exits 3 right after acknowledging ``prompt``.
- ``fail_setup``: rejects ``open_session``.

``FAKE_OMP_BIG=N`` pads the final text to N characters (v2 sends it as ``rpc_chunk`` frames of
``FAKE_OMP_CHUNK`` bytes). Every received frame is appended to ``FAKE_OMP_LOG`` when set.
"""

import base64
import json
import os
import sys

MODE = os.environ.get("FAKE_OMP_MODE", "ok")
BIG = int(os.environ.get("FAKE_OMP_BIG", "0"))
CHUNK = int(os.environ.get("FAKE_OMP_CHUNK", "4096"))
LOG = os.environ.get("FAKE_OMP_LOG")

state = {"v2": False, "chunks": 0, "results": [], "prompt": None, "hanging": False}


def emit(frame: dict) -> None:
    line = json.dumps(frame, separators=(",", ":"))
    data = line.encode()
    if state["v2"] and len(data) > CHUNK:
        state["chunks"] += 1
        parts = [data[i : i + CHUNK] for i in range(0, len(data), CHUNK)]
        for index, part in enumerate(parts):
            chunk = {
                "type": "rpc_chunk",
                "chunkId": f"rpc-{state['chunks']}",
                "index": index,
                "count": len(parts),
                "byteLength": len(data),
                "data": base64.b64encode(part).decode(),
            }
            sys.stdout.write(json.dumps(chunk) + "\n")
    else:
        sys.stdout.write(line + "\n")
    sys.stdout.flush()


def respond(frame: dict, data: object = None, *, ok: bool = True, error: str = "") -> None:
    out: dict = {"type": "response", "command": frame.get("type"), "success": ok}
    if "id" in frame:
        out["id"] = frame["id"]
    if ok and data is not None:
        out["data"] = data
    if not ok:
        out["error"] = error
    emit(out)


def finish(status: str, settled: bool) -> None:
    emit(
        {
            "type": "prompt_result",
            "id": state["prompt"],
            "agentInvoked": True,
            "status": status,
            "sessionSettled": settled,
        }
    )
    if not settled:
        emit({"type": "session_settled"})


def handle(frame: dict) -> None:
    kind = frame.get("type")
    if kind == "negotiate_protocol":
        respond(frame, {"protocolVersion": 2})
        state["v2"] = True
    elif kind == "set_cache_warming":
        respond(frame, {"mode": frame.get("mode")})
    elif kind == "set_event_filter":
        respond(frame, {"events": frame.get("events"), "messageUpdates": "full"})
    elif kind == "set_host_tools":
        respond(frame, {"toolNames": [t["name"] for t in frame.get("tools", [])]})
    elif kind == "open_session":
        if MODE == "fail_setup":
            respond(frame, ok=False, error="Model not found: ygg/nope")
        else:
            respond(frame, {"cancelled": False, "resumed": False, "sessionId": "s1", "sessionFile": "/tmp/s1.jsonl"})
    elif kind == "prompt":
        if state["prompt"] is not None:
            # Like omp while the agent is streaming: a second prompt needs streamingBehavior.
            respond(frame, ok=False, error="streamingBehavior is required while the agent is streaming")
            return
        state["prompt"] = frame.get("id")
        respond(frame)
        emit({"type": "agent_start"})
        emit({"type": "tool_execution_start", "toolCallId": "t1", "toolName": "bash", "args": {"command": "ls -la"}})
        if MODE == "crash":
            sys.stderr.write("boom: provider exploded\n")
            sys.stderr.flush()
            sys.exit(3)
        if MODE == "hang":
            state["hanging"] = True
            return
        emit(
            {
                "type": "host_tool_call",
                "id": "host_1",
                "toolCallId": "toolu_1",
                "toolName": "show",
                "arguments": {"path": "/workspace/site/shot.png", "caption": "首页截图"},
            }
        )
    elif kind == "host_tool_result":
        if any(r.split(":", 1)[0].rstrip("!") == frame.get("id") for r in state["results"]):
            return  # like omp: a result for a call that is no longer pending is dropped
        text = "".join(c.get("text", "") for c in frame.get("result", {}).get("content", []))
        state["results"].append(f"{frame.get('id')}{'!' if frame.get('isError') else ''}:{text}")
        if frame.get("id") == "host_1":
            emit(
                {
                    "type": "host_tool_call",
                    "id": "host_2",
                    "toolCallId": "toolu_2",
                    "toolName": "progress",
                    "arguments": {"text": "做到一半"},
                }
            )
        elif frame.get("id") == "host_2":
            emit({"type": "tool_execution_end", "toolCallId": "t1", "toolName": "bash", "isError": True})
            finish("completed", settled=False)
    elif kind == "abort":
        respond(frame)
        if state["hanging"]:
            state["hanging"] = False
            finish("aborted", settled=True)
    elif kind == "get_last_assistant_text":
        text = "完成了。" + " | ".join(state["results"])
        if BIG:
            text += "x" * max(0, BIG - len(text))
        respond(frame, {"text": text})
    else:
        respond(frame, ok=False, error=f"unknown command {kind}")


def main() -> None:
    sys.stdin.reconfigure(encoding="utf-8")  # pyright: ignore[reportAttributeAccessIssue]
    sys.stdout.reconfigure(encoding="utf-8")  # pyright: ignore[reportAttributeAccessIssue]
    emit(
        {
            "type": "ready",
            "protocolVersion": 1,
            "supportedProtocolVersions": [1, 2],
            "maxFrameBytes": 1048576,
            "maxReassembledFrameBytes": 67108864,
        }
    )
    for raw in sys.stdin:
        if not raw.strip():
            continue
        frame = json.loads(raw)
        if LOG:
            with open(LOG, "a", encoding="utf-8") as f:
                f.write(json.dumps(frame, ensure_ascii=False) + "\n")
        handle(frame)
    sys.exit(0)


if __name__ == "__main__":
    main()
