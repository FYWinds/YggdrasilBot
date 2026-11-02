"""Fake HTTP upstreams for httpx."""

import inspect
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

import httpx
import pytest

type Answer = httpx.Response | Callable[[httpx.Request], httpx.Response | Awaitable[httpx.Response]]


class Routes:
    """Answers httpx requests by URL without its query (``"https://api.example.com/v1/models"``), else by host
    (``"api.example.com"``): a response, or a function of the request that returns one (it may be async). Anything
    else gets a 404. Every request is kept in ``seen``.

    ``client()`` is a client for code that takes one; ``install(monkeypatch)`` answers the requests of every client
    the code builds itself (httpx's default transport)."""

    def __init__(self, answers: Mapping[str, Answer] | None = None) -> None:
        self.answers: dict[str, Answer] = dict(answers or {})
        self.seen: list[httpx.Request] = []

    async def handle(self, request: httpx.Request) -> httpx.Response:
        self.seen.append(request)
        answer = self.answers.get(str(request.url.copy_with(query=None)))
        if answer is None:
            answer = self.answers.get(request.url.host)
        if answer is None:
            return httpx.Response(404, json={"error": f"no route for {request.url}"})
        if isinstance(answer, httpx.Response):
            return answer
        response = answer(request)
        return await response if inspect.isawaitable(response) else response

    def client(self, **kwargs: Any) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handle), **kwargs)

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def handle(_transport: httpx.AsyncHTTPTransport, request: httpx.Request) -> httpx.Response:
            return await self.handle(request)

        monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", handle)
