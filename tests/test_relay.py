import asyncio
import inspect
import json
import logging
import socket
from collections.abc import AsyncIterator, Awaitable, Callable

import fakeredis
import httpx
import pytest

from yggdrasil import runtime
from yggdrasil.config import APP_CONFIG
from yggdrasil.llm.catalog import ModelCaps
from yggdrasil.relay import grants, issue_token, relay_models, relay_url, relay_usage, revoke_token
from yggdrasil.relay.grants import authenticate, pending_key, token_key
from yggdrasil.relay.server import Relay, _clamp, _SseUsage, start_relay, stop_relay
from yggdrasil.runtime import ProviderInfo, Route, Snapshot

REAL_KEY = "sk-real-provider-key"
MODEL = "claude-opus-5-5"
FALLBACK = "claude-sonnet-5-5"
CAPS = ModelCaps(
    catalog_id="anthropic/claude-opus-5-5", vision=True, pdf=True, tools=True, reasoning=True, context=500_000
)

SSE = (
    b"event: message_start\n"
    b'data: {"type":"message_start","message":{"id":"msg_1","type":"message","role":"assistant","model":"'
    + MODEL.encode()
    + b'","content":[],"usage":{"input_tokens":100,"cache_creation_input_tokens":20,'
    b'"cache_read_input_tokens":300,"output_tokens":1}}}\n\n'
    b"event: content_block_start\n"
    b'data: {"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}\n\n'
    b"event: content_block_delta\n"
    b'data: {"type":"content_block_delta","index":0,'
    b'"delta":{"type":"text_delta","text":"\xe4\xbd\xa0\xe5\xa5\xbd"}}\n\n'
    b"event: content_block_stop\n"
    b'data: {"type":"content_block_stop","index":0}\n\n'
    b"event: message_delta\n"
    b'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"},'
    b'"usage":{"input_tokens":0,"output_tokens":42}}\n\n'
    b"event: message_stop\n"
    b'data: {"type":"message_stop"}\n\n'
)


def _snapshot(*, fallback_format: str = "anthropic") -> Snapshot:
    primary = ProviderInfo(
        1,
        "gw",
        "https://gw.example.com/v1",
        REAL_KEY,
        {"x-provider": "p1"},
        True,
        (MODEL,),
        api_format="anthropic",
        caps={MODEL: CAPS},
    )
    backup = ProviderInfo(
        2, "backup", "https://backup.example.com", "sk-backup", {}, True, (FALLBACK,), fallback_format
    )
    route = Route("chat", 1, MODEL, {}, 2, FALLBACK, {})
    return Snapshot(providers={1: primary, 2: backup}, routes={"chat": route})


@pytest.fixture
async def redis(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[fakeredis.FakeAsyncRedis]:
    conn = fakeredis.FakeAsyncRedis(decode_responses=True)
    monkeypatch.setattr(grants, "redis_client", lambda: conn)
    monkeypatch.setattr(runtime, "_snapshot", _snapshot())
    yield conn
    await conn.aclose()


class Upstream:
    """Fake provider: records requests, answers per host with a handler."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.answer: dict[str, Callable[[httpx.Request], httpx.Response | Awaitable[httpx.Response]]] = {}

    async def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        answer = self.answer[request.url.host](request)
        return await answer if inspect.isawaitable(answer) else answer


@pytest.fixture
async def upstream() -> Upstream:
    return Upstream()


@pytest.fixture
async def client(redis: fakeredis.FakeAsyncRedis, upstream: Upstream) -> AsyncIterator[httpx.AsyncClient]:
    http = httpx.AsyncClient(transport=httpx.MockTransport(upstream.handler))
    app = Relay(http).app()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://relay.test") as c:
        yield c
    await http.aclose()


async def _token(job_id: str = "job1", **kw: int) -> str:
    args = {"max_input_tokens": 1_000_000, "max_output_tokens": 100_000, "ttl_s": 600, **kw}
    return await issue_token(group_id=10001, job_id=job_id, models=[MODEL], **args)


def _sse_response(_request: httpx.Request) -> httpx.Response:
    async def chunks() -> AsyncIterator[bytes]:
        for i in range(0, len(SSE), 37):  # split mid-line and mid-character
            yield SSE[i : i + 37]

    return httpx.Response(200, headers={"content-type": "text/event-stream", "request-id": "req_1"}, content=chunks())


async def test_token_lifecycle(redis: fakeredis.FakeAsyncRedis) -> None:
    token = await _token(ttl_s=600)
    assert len(token) >= 40
    grant = await authenticate(token)
    assert grant is not None
    assert (grant.group_id, grant.job_id, grant.models) == (10001, "job1", (MODEL,))
    # Only the hash is stored, and it expires with the grant.
    keys = await redis.keys("ygg:relay:*")
    assert all(token not in str(k) for k in keys)
    values = [await redis.get(k) for k in keys]
    assert all(token not in str(v or "") for v in values)
    assert 0 < await redis.ttl(token_key(grants.digest(token))) <= 600

    reissued = await _token()
    assert await authenticate(token) is None  # the job's previous token stops working
    assert await authenticate(reissued) is not None

    await revoke_token("job1")
    assert await authenticate(reissued) is None
    assert await redis.keys("ygg:relay:*") == []
    await revoke_token("job1")  # idempotent


async def test_expired_grant_is_rejected(redis: fakeredis.FakeAsyncRedis) -> None:
    token = await _token()
    key = token_key(grants.digest(token))
    data = json.loads(await redis.get(key) or "{}")
    await redis.set(key, json.dumps({**data, "expires_at": data["expires_at"] - 3600}))
    assert await authenticate(token) is None


async def test_issue_rejects_empty_grants(redis: fakeredis.FakeAsyncRedis) -> None:
    with pytest.raises(ValueError, match="model"):
        await issue_token(group_id=1, job_id="j", models=[], max_input_tokens=1, max_output_tokens=1, ttl_s=60)
    with pytest.raises(ValueError, match="ttl"):
        await issue_token(group_id=1, job_id="j", models=[MODEL], max_input_tokens=1, max_output_tokens=1, ttl_s=0)


async def test_relay_models_come_from_the_chat_route(redis: fakeredis.FakeAsyncRedis) -> None:
    models = await relay_models()
    assert [m.id for m in models] == [MODEL, FALLBACK]
    opus = models[0]
    assert (opus.name, opus.context_window, opus.reasoning, opus.vision) == (
        "Anthropic/claude-opus-5-5",
        500_000,
        True,
        True,
    )
    assert opus.max_tokens > 0  # unknown output limit → default
    assert not models[1].vision


async def test_relay_models_skip_openai_format_providers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runtime, "_snapshot", _snapshot(fallback_format="openai"))
    assert [m.id for m in await relay_models()] == [MODEL]


async def test_requests_without_valid_token_are_rejected(client: httpx.AsyncClient) -> None:
    body = {"model": MODEL, "max_tokens": 10, "messages": []}
    r = await client.post("/v1/messages", json=body)
    assert r.status_code == 401
    assert r.json()["type"] == "error"
    assert r.json()["error"]["type"] == "authentication_error"
    r = await client.post("/v1/messages", json=body, headers={"authorization": "Bearer nope"})
    assert r.status_code == 401


async def test_model_outside_the_grant_is_forbidden(client: httpx.AsyncClient, upstream: Upstream) -> None:
    token = await _token()
    r = await client.post("/v1/messages", json={"model": FALLBACK, "messages": []}, headers={"x-api-key": token})
    assert r.status_code == 403
    assert r.json()["error"]["type"] == "permission_error"
    assert upstream.requests == []


async def test_exhausted_budget_is_rejected(client: httpx.AsyncClient, upstream: Upstream) -> None:
    upstream.answer["gw.example.com"] = _sse_response
    token = await _token(max_output_tokens=40)
    headers = {"authorization": f"Bearer {token}"}
    body = {"model": MODEL, "stream": True, "messages": []}
    first = await client.post("/v1/messages", json=body, headers=headers)
    assert first.status_code == 200  # under budget when it started; the stream is not cut
    second = await client.post("/v1/messages", json=body, headers=headers)
    assert second.status_code == 429
    assert second.json()["error"]["type"] == "rate_limit_error"
    assert len(upstream.requests) == 1


async def test_stream_passes_through_and_usage_is_recorded(client: httpx.AsyncClient, upstream: Upstream) -> None:
    upstream.answer["gw.example.com"] = _sse_response
    token = await _token()
    body = json.dumps({"model": MODEL, "stream": True, "max_tokens": 100, "messages": []})
    async with client.stream(
        "POST",
        "/v1/messages",
        content=body,
        headers={
            "authorization": f"Bearer {token}",
            "anthropic-version": "2023-06-01",
            "anthropic-beta": "interleaved-thinking-2025-05-14",
            "content-type": "application/json",
            "x-forwarded-for": "10.0.0.1",
            "cookie": "a=b",
        },
    ) as r:
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/event-stream")
        assert r.headers["request-id"] == "req_1"
        received = b"".join([chunk async for chunk in r.aiter_bytes()])
    assert received == SSE

    assert await relay_usage("job1") == grants.RelayUsage(
        requests=1, input_tokens=420, cached_tokens=300, output_tokens=42
    )

    (sent,) = upstream.requests
    assert str(sent.url) == "https://gw.example.com/v1/messages"
    assert sent.content == body.encode()  # forwarded unchanged
    assert sent.headers["x-api-key"] == REAL_KEY
    assert sent.headers["host"] == "gw.example.com"
    assert sent.headers["anthropic-beta"] == "interleaved-thinking-2025-05-14"
    assert sent.headers["x-provider"] == "p1"
    assert "authorization" not in sent.headers
    assert "cookie" not in sent.headers
    assert "x-forwarded-for" not in sent.headers
    assert all(token not in v for v in sent.headers.values())


async def test_non_streaming_usage_is_recorded(client: httpx.AsyncClient, upstream: Upstream) -> None:
    message = {
        "id": "msg_2",
        "type": "message",
        "content": [{"type": "text", "text": "hi"}],
        "usage": {"input_tokens": 7, "cache_read_input_tokens": 3, "output_tokens": 5},
    }
    upstream.answer["gw.example.com"] = lambda _r: httpx.Response(200, json=message)
    token = await _token()
    r = await client.post("/v1/messages", json={"model": MODEL, "messages": []}, headers={"x-api-key": token})
    assert r.status_code == 200
    assert r.json() == message
    assert upstream.requests[0].headers["anthropic-version"] == "2023-06-01"  # default when the client sent none
    assert await relay_usage("job1") == grants.RelayUsage(requests=1, input_tokens=10, cached_tokens=3, output_tokens=5)


async def test_primary_failure_falls_back_with_the_fallback_model(
    client: httpx.AsyncClient, upstream: Upstream
) -> None:
    upstream.answer["gw.example.com"] = lambda _r: httpx.Response(529, json={"type": "error"})
    upstream.answer["backup.example.com"] = _sse_response
    token = await _token()
    r = await client.post(
        "/v1/messages", json={"model": MODEL, "stream": True, "messages": []}, headers={"x-api-key": token}
    )
    assert r.status_code == 200
    assert r.content == SSE
    primary, backup = upstream.requests
    assert json.loads(backup.content)["model"] == FALLBACK
    assert str(backup.url) == "https://backup.example.com/v1/messages"
    assert backup.headers["x-api-key"] == "sk-backup"
    assert primary.headers["x-api-key"] == REAL_KEY


async def test_upstream_client_errors_pass_through_unaccounted(client: httpx.AsyncClient, upstream: Upstream) -> None:
    err = {"type": "error", "error": {"type": "invalid_request_error", "message": "bad"}}
    upstream.answer["gw.example.com"] = lambda _r: httpx.Response(400, json=err)
    token = await _token()
    r = await client.post("/v1/messages", json={"model": MODEL, "messages": []}, headers={"x-api-key": token})
    assert (r.status_code, r.json()) == (400, err)
    assert len(upstream.requests) == 1  # a 400 is not retried on the fallback
    assert (await relay_usage("job1")).requests == 0


async def test_unreachable_upstreams_give_an_api_error(client: httpx.AsyncClient, upstream: Upstream) -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    upstream.answer["gw.example.com"] = refuse
    upstream.answer["backup.example.com"] = refuse
    token = await _token()
    r = await client.post("/v1/messages", json={"model": MODEL, "messages": []}, headers={"x-api-key": token})
    assert r.status_code == 502
    assert r.json()["error"]["type"] == "api_error"


async def test_models_lists_the_grant(client: httpx.AsyncClient) -> None:
    token = await _token()
    r = await client.get("/v1/models", headers={"authorization": f"Bearer {token}"})
    assert r.status_code == 200
    assert [m["id"] for m in r.json()["data"]] == [MODEL]
    assert (await client.get("/v1/models")).status_code == 401


def test_sse_usage_survives_any_chunking() -> None:
    sse = _SseUsage()
    for i in range(len(SSE)):
        sse.feed(SSE[i : i + 1])
    assert (sse.usage.total_input, sse.usage.cache_read, sse.usage.output) == (420, 300, 42)


async def test_parallel_requests_cannot_overrun_the_output_budget(
    client: httpx.AsyncClient, upstream: Upstream, redis: fakeredis.FakeAsyncRedis
) -> None:
    both_upstream = asyncio.Event()

    async def slow(request: httpx.Request) -> httpx.Response:
        if len(upstream.requests) == 2:
            both_upstream.set()
        await both_upstream.wait()  # both are in flight before either records usage
        max_tokens = json.loads(request.content)["max_tokens"]
        return httpx.Response(200, json={"usage": {"input_tokens": 1, "output_tokens": max_tokens}})

    upstream.answer["gw.example.com"] = slow
    token = await _token(max_output_tokens=100)
    body = {"model": MODEL, "max_tokens": 80, "messages": []}
    responses = await asyncio.gather(
        *(client.post("/v1/messages", json=body, headers={"x-api-key": token}) for _ in range(3))
    )
    assert sorted(r.status_code for r in responses) == [200, 200, 429]
    # The second request was clamped to what the first one's reservation left.
    assert sorted(json.loads(r.content)["max_tokens"] for r in upstream.requests) == [20, 80]
    assert (await relay_usage("job1")).output_tokens == 100
    assert await redis.hgetall(pending_key("job1")) == {}  # pyright: ignore[reportGeneralTypeIssues]


async def test_failed_requests_release_their_reservation(
    client: httpx.AsyncClient, upstream: Upstream, redis: fakeredis.FakeAsyncRedis
) -> None:
    upstream.answer["gw.example.com"] = lambda _r: httpx.Response(400, json={"type": "error"})
    token = await _token(max_output_tokens=100)
    for _ in range(3):
        r = await client.post(
            "/v1/messages", json={"model": MODEL, "max_tokens": 80, "messages": []}, headers={"x-api-key": token}
        )
        assert r.status_code == 400
    assert await redis.hgetall(pending_key("job1")) == {}  # pyright: ignore[reportGeneralTypeIssues]


async def test_lapsed_reservations_stop_counting(
    client: httpx.AsyncClient, upstream: Upstream, redis: fakeredis.FakeAsyncRedis
) -> None:
    upstream.answer["gw.example.com"] = lambda r: httpx.Response(200, json={"usage": {"output_tokens": 1}})
    token = await _token(max_output_tokens=100)
    await redis.hset(pending_key("job1"), mapping={"dead": "100:1", "garbage": "x"})
    r = await client.post(
        "/v1/messages", json={"model": MODEL, "max_tokens": 50, "messages": []}, headers={"x-api-key": token}
    )
    assert r.status_code == 200
    assert json.loads(upstream.requests[0].content)["max_tokens"] == 50
    assert await redis.hgetall(pending_key("job1")) == {}


def test_clamp_keeps_thinking_budget_below_max_tokens() -> None:
    payload = {"model": MODEL, "max_tokens": 32000, "thinking": {"type": "enabled", "budget_tokens": 16000}}
    assert _clamp(payload, 20000)["thinking"]["budget_tokens"] == 16000
    clamped = _clamp(payload, 8000)
    assert (clamped["max_tokens"], clamped["thinking"]["budget_tokens"]) == (8000, 7999)


async def test_stream_cut_before_message_delta_charges_an_estimate(
    client: httpx.AsyncClient, upstream: Upstream
) -> None:
    cut = SSE[: SSE.index(b"event: message_delta")] + (
        b"event: content_block_delta\n"
        b'data: {"type":"content_block_delta","index":0,"delta":{"type":"thinking_delta","thinking":"'
        + b"x"
        * 600
        + b'"}}\n\n'
        b"event: content_block_delta\n"
        b'data: {"type":"content_block_delta","index":1,"delta":{"type":"input_json_delta","partial_json":"'
        + b"y" * 298
        + b'"}}\n\n'
    )
    upstream.answer["gw.example.com"] = lambda _r: httpx.Response(
        200, headers={"content-type": "text/event-stream"}, content=cut
    )
    token = await _token()
    r = await client.post(
        "/v1/messages", json={"model": MODEL, "stream": True, "messages": []}, headers={"x-api-key": token}
    )
    assert r.content == cut
    # 2 + 600 + 298 generated characters → 300 tokens, though message_start only reported 1.
    assert (await relay_usage("job1")).output_tokens == 300


def test_sse_estimate_never_undercuts_message_start() -> None:
    sse = _SseUsage()
    sse.feed(SSE[: SSE.index(b"event: content_block_start")])
    assert sse.usage.output == 1


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def relay_config(monkeypatch: pytest.MonkeyPatch) -> int:
    port = _free_port()
    monkeypatch.setattr(APP_CONFIG, "relay_url", "http://10.0.0.1:8090/")
    monkeypatch.setattr(APP_CONFIG, "relay_port", port)
    monkeypatch.setattr(APP_CONFIG, "host", "127.0.0.1")
    return port


async def test_relay_server_serves_without_touching_shared_logging(relay_config: int) -> None:
    sentinel = logging.NullHandler()
    access = logging.getLogger("uvicorn.access")
    access.addHandler(sentinel)
    loggers = [logging.getLogger(n) for n in ("uvicorn", "uvicorn.access", "uvicorn.error", "uvicorn.asgi")]
    before = [(lg.handlers[:], lg.propagate, lg.level, lg.filters[:]) for lg in loggers]
    try:
        assert relay_url() is None  # configured but not serving yet
        await start_relay()
        assert relay_url() == "http://10.0.0.1:8090"
        async with httpx.AsyncClient() as http:
            r = await http.get(f"http://127.0.0.1:{relay_config}/v1/models")
        assert r.status_code == 401
        assert [(lg.handlers[:], lg.propagate, lg.level, lg.filters[:]) for lg in loggers] == before
    finally:
        await stop_relay()
        access.removeHandler(sentinel)
    assert relay_url() is None


async def test_relay_that_cannot_bind_stays_disabled(relay_config: int) -> None:
    with socket.socket() as taken:
        taken.bind(("127.0.0.1", relay_config))
        taken.listen()
        await start_relay()  # logs, doesn't raise or exit
        assert relay_url() is None
        await stop_relay()
