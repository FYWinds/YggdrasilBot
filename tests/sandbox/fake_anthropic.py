"""A scripted Anthropic Messages endpoint for running the real omp inside a sandbox without a model provider.

``python3 fake_anthropic.py PORT LOG``: the first ``POST /v1/messages`` of a conversation answers with a
``show`` tool call, any request that already carries a tool result answers with a final text. Each request's
auth header, model and tool names are appended to LOG as one JSON line. Streaming (SSE) and plain JSON both
work. Standard library only (it runs on the workspace image's python3).
"""

import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(sys.argv[1])
LOG = sys.argv[2]
FINAL = "完成了：页面已截图。"


def _has_tool_result(body: dict) -> bool:
    """Whether the newest message answers a tool call (a resumed session carries older ones in its history)."""
    messages = body.get("messages") or [{}]
    content = messages[-1].get("content")
    return isinstance(content, list) and any(isinstance(c, dict) and c.get("type") == "tool_result" for c in content)


def _blocks(body: dict) -> tuple[list[dict], str]:
    if _has_tool_result(body):
        return [{"type": "text", "text": FINAL}], "end_turn"
    tool = {
        "type": "tool_use",
        "id": "toolu_fake_1",
        "name": "show",
        "input": {"path": "/workspace/site/shot.png", "caption": "首页"},
    }
    return [tool], "tool_use"


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        return

    def _json(self, status: int, data: dict) -> None:
        raw = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self) -> None:
        if self.path.rstrip("/").endswith("/v1/models"):
            self._json(200, {"data": [{"id": "claude-a", "type": "model", "display_name": "Claude A"}]})
        else:
            self._json(404, {"type": "error", "error": {"type": "not_found_error", "message": self.path}})

    def do_POST(self) -> None:
        length = int(self.headers.get("content-length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        if not self.path.split("?")[0].endswith("/v1/messages"):
            self._json(404, {"type": "error", "error": {"type": "not_found_error", "message": self.path}})
            return
        with open(LOG, "a", encoding="utf-8") as f:
            entry = {
                "authorization": self.headers.get("authorization"),
                "x-api-key": self.headers.get("x-api-key"),
                "model": body.get("model"),
                "stream": body.get("stream"),
                "tools": [t.get("name") for t in body.get("tools", [])],
            }
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        blocks, stop = _blocks(body)
        usage = {
            "input_tokens": 100,
            "output_tokens": 20,
            "cache_read_input_tokens": 0,
            "cache_creation_input_tokens": 0,
        }
        message = {
            "id": "msg_fake",
            "type": "message",
            "role": "assistant",
            "model": body.get("model", "claude-a"),
            "content": blocks,
            "stop_reason": stop,
            "stop_sequence": None,
            "usage": usage,
        }
        if not body.get("stream"):
            self._json(200, message)
            return
        self.send_response(200)
        self.send_header("content-type", "text/event-stream")
        self.send_header("cache-control", "no-cache")
        self.end_headers()

        def event(kind: str, data: dict) -> None:
            self.wfile.write(f"event: {kind}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n".encode())
            self.wfile.flush()

        start = {**message, "content": [], "stop_reason": None, "usage": {**usage, "output_tokens": 1}}
        event("message_start", {"type": "message_start", "message": start})
        for i, block in enumerate(blocks):
            if block["type"] == "text":
                event(
                    "content_block_start",
                    {"type": "content_block_start", "index": i, "content_block": {"type": "text", "text": ""}},
                )
                delta = {"type": "text_delta", "text": block["text"]}
            else:
                head = {**block, "input": {}}
                event("content_block_start", {"type": "content_block_start", "index": i, "content_block": head})
                delta = {"type": "input_json_delta", "partial_json": json.dumps(block["input"], ensure_ascii=False)}
            event("content_block_delta", {"type": "content_block_delta", "index": i, "delta": delta})
            event("content_block_stop", {"type": "content_block_stop", "index": i})
        event(
            "message_delta",
            {
                "type": "message_delta",
                "delta": {"stop_reason": stop, "stop_sequence": None},
                "usage": {"output_tokens": 20},
            },
        )
        event("message_stop", {"type": "message_stop"})


if __name__ == "__main__":
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
