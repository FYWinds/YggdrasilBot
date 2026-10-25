import httpx
import pytest

from yggdrasil.browser import service
from yggdrasil.tools.errors import ToolError


class _Engine:
    def __init__(self, answer: tuple[str, str] | Exception) -> None:
        self.answer = answer
        self.urls: list[str] = []

    async def render(self, url: str, timeout_s: float) -> tuple[str, str]:
        self.urls.append(url)
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer

    async def close(self) -> None:
        return None


async def _post(engines: dict[str, service.Engine], payload: object) -> httpx.Response:
    transport = httpx.ASGITransport(app=service.create_app(engines))
    async with httpx.AsyncClient(transport=transport, base_url="http://browser") as client:
        return await client.post("/render", json=payload)


async def test_render_returns_the_engine_result() -> None:
    engine = _Engine(("http://93.184.215.14/final", "<html>ok</html>"))
    resp = await _post({"patchright": engine}, {"url": "http://93.184.215.14/a", "engine": "patchright"})
    assert resp.status_code == 200
    assert resp.json() == {"final_url": "http://93.184.215.14/final", "html": "<html>ok</html>"}
    assert engine.urls == ["http://93.184.215.14/a"]


async def test_disallowed_url_is_a_400_and_never_reaches_the_engine() -> None:
    engine = _Engine(("x", "y"))
    resp = await _post({"patchright": engine}, {"url": "http://127.0.0.1:8080/", "engine": "patchright"})
    assert resp.status_code == 400
    assert "内网" in resp.json()["error"]
    assert engine.urls == []


@pytest.mark.parametrize(
    ("payload", "status"),
    [({"url": "http://93.184.215.14/", "engine": "selenium"}, 422), ({"engine": "patchright"}, 422)],
)
async def test_bad_requests_are_422(payload: object, status: int) -> None:
    assert (await _post({"patchright": _Engine(("x", "y"))}, payload)).status_code == status


async def test_engine_failure_is_a_502() -> None:
    engine = _Engine(RuntimeError("chrome died"))
    resp = await _post({"nodriver": engine}, {"url": "http://93.184.215.14/", "engine": "nodriver"})
    assert resp.status_code == 502


async def test_verdicts_vet_origins_once_and_refuse_local_schemes(monkeypatch: pytest.MonkeyPatch) -> None:
    checked: list[str] = []

    async def check_url(url: str) -> str:
        checked.append(url)
        if "internal" in url:
            raise ToolError("不允许访问内网或保留地址")
        return url

    monkeypatch.setattr(service, "check_url", check_url)
    verdicts = service.Verdicts()
    assert await verdicts.allowed("https://cdn.example.com/a.js")
    assert await verdicts.allowed("https://cdn.example.com/b.css?x=1")
    assert not await verdicts.allowed("http://internal.example.com/admin")
    assert await verdicts.allowed("data:text/plain,hi")
    assert not await verdicts.allowed("file:///etc/passwd")
    assert checked == ["https://cdn.example.com/", "http://internal.example.com/"]
