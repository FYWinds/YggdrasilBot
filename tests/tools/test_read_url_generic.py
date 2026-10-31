"""read_url: JSON compaction, URL fragments, pagination of code, fetch deadlines, and the Statuspage / Ubuntu CVE /
Hugging Face / MSN adapters."""

import asyncio
import json
import time
from collections.abc import AsyncGenerator, AsyncIterator, Callable
from contextlib import asynccontextmanager

import httpx
import pytest

from yggdrasil.tools import web
from yggdrasil.tools.errors import ToolError
from yggdrasil.tools.ssrf import GuardedResponse
from yggdrasil.tools.web import Extracted, paginate, read_url

type Handler = Callable[[httpx.Request], httpx.Response]


def _client(handler: Handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.fixture
def serve(monkeypatch: pytest.MonkeyPatch) -> Callable[[Handler], None]:
    """Route the generic path's impersonated fetch to ``handler``; ``check_url`` only drops the fragment."""

    async def check(url: str) -> str:
        return url.partition("#")[0]

    def install(handler: Handler) -> None:
        @asynccontextmanager
        async def fake(url: str, **_: object) -> AsyncGenerator[GuardedResponse]:
            resp = handler(httpx.Request("GET", url))

            async def chunks() -> AsyncIterator[bytes]:
                yield resp.content

            lowered = {k.lower(): v for k, v in resp.headers.items()}
            yield GuardedResponse(url, resp.status_code, lowered, resp.charset_encoding, chunks())

        monkeypatch.setattr(web, "impersonated_stream", fake)

    monkeypatch.setattr(web, "check_url", check)
    web._cache.clear()
    return install


@pytest.fixture
def adapter_text(monkeypatch: pytest.MonkeyPatch) -> Callable[[str], None]:
    """Make every read return ``text`` the way an adapter does (no page source)."""

    async def check(url: str) -> str:
        return url.partition("#")[0]

    def install(text: str) -> None:
        async def fetch(url: str, browser_url: str | None, timeout_s: float) -> Extracted:
            return Extracted(url, "acme/widget", text, "github")

        monkeypatch.setattr(web, "_fetch", fetch)

    monkeypatch.setattr(web, "check_url", check)
    web._cache.clear()
    return install


# --------------------------------------------------------------------------- JSON


def _commit(sha: str, message: str) -> dict:
    user = {
        "login": "octocat",
        "id": 10001,
        "node_id": "MDQ6VXNlcjEwMDAx",
        "avatar_url": "https://avatars.example.com/u/10001?v=4",
        "gravatar_id": "",
        "url": "https://api.example.com/users/octocat",
        "html_url": "https://example.com/octocat",
        "followers_url": "https://api.example.com/users/octocat/followers",
        "following_url": "https://api.example.com/users/octocat/following{/other_user}",
        "type": "User",
    }
    return {
        "sha": sha,
        "node_id": "C_kwDOAAAB",
        "commit": {
            "author": {"name": "Octo Cat", "email": "octo@example.com", "date": "2026-01-02T03:04:05Z"},
            "message": message,
            "url": f"https://api.example.com/repos/acme/widget/git/commits/{sha}",
            "comment_count": 0,
            "verification": {
                "verified": True,
                "reason": "valid",
                "signature": "-----BEGIN PGP SIGNATURE-----\n\niHUEABYKAB0WIQ\n-----END PGP SIGNATURE-----",
                "payload": f"tree {sha}\nauthor Octo Cat <octo@example.com> 1767323045 +0000\n\n{message}",
            },
        },
        "html_url": f"https://example.com/acme/widget/commit/{sha}",
        "author": user,
        "parents": [],
        "homepage": None,
    }


@pytest.mark.parametrize("ctype", ["application/json; charset=utf-8", "application/vnd.github+json"])
async def test_json_api_response_keeps_values_and_drops_noise(serve: Callable[[Handler], None], ctype: str) -> None:
    payload = [_commit("a" * 40, "fix: handle empty input"), _commit("b" * 40, "docs: usage\n\nMore detail.")]
    serve(
        lambda r: httpx.Response(200, content=json.dumps(payload, indent=2).encode(), headers={"content-type": ctype})
    )
    result = await web._fetch_generic("https://api.example.com/repos/acme/widget/commits", None, 5)
    assert result.via == "document:json"
    note, text = result.text.split("\n\n", 1)
    assert note == "（JSON 数据，已精简：去掉了空值、node_id、头像、API 链接和签名等字段）"
    for kept in (
        "- sha: " + "a" * 40,
        "message: fix: handle empty input",
        'message: "docs: usage\\n\\nMore detail."',
        "verification: {verified: true, reason: valid}",
        "author: {login: octocat, id: 10001, html_url: https://example.com/octocat, type: User}",
        "comment_count: 0",
    ):
        assert kept in text
    for noise in ("node_id", "avatar", "gravatar", "followers_url", "api.example.com", "PGP", "tree aaaa", "null"):
        assert noise not in text
    assert f"\n\n- sha: {'b' * 40}" in text  # one paragraph per item: pages break between items
    assert len(text) < len(json.dumps(payload, indent=2)) / 2


async def test_compacted_json_keeps_every_list_element(serve: Callable[[Handler], None]) -> None:
    payload = {"t": [1, 2, 3, 4], "v": [10.5, None, 0, 7], "rows": [["a", None], [], {}], "gone": None, "none": []}
    serve(lambda r: httpx.Response(200, json=payload))
    result = await web._fetch_generic("https://api.example.com/series", None, 5)
    text = result.text.split("\n\n", 1)[1]
    assert text == "t: [1, 2, 3, 4]\nv: [10.5, null, 0, 7]\nrows:\n  - [a, null]\n  - []\n  - {}"


def test_pagination_keeps_indentation_of_code_after_blank_lines() -> None:
    code = "class A:\n    def f(self):\n        pass\n\n    def g(self):\n        pass"
    assert paginate(code, 1000) == [code]
    assert paginate(code, 30) == ["class A:\n    def f(self):", "        pass", "    def g(self):\n        pass"]


# --------------------------------------------------------------------------- URL fragments

SPEC = """<html><head><title>Example Spec</title></head><body><main>
<nav><ul><li><a href="#section-1">1. Background</a></li><li><a href="#section-2">2. Timestamps</a></li></ul></nav>
<section id="section-1"><h2 id="name-background"><a href="#section-1">1. </a><a href="#name-background">Background</a>
</h2>{first}</section>
<section id="section-2"><h2 id="name-timestamps"><a href="#section-2">2. </a><a href="#name-timestamps">Timestamps</a>
</h2>{second}</section>
</main></body></html>""".format(
    first="".join(f"<p>第一节正文第 {i} 段，介绍背景和术语，内容足够长，抽取器会保留它。</p>" for i in range(8)),
    second="".join(f"<p>第二节正文第 {i} 段，说明时间戳的编码方式，内容同样足够长。</p>" for i in range(8)),
)


async def test_fragment_starts_the_pages_at_its_section(serve: Callable[[Handler], None]) -> None:
    serve(lambda r: httpx.Response(200, html=SPEC))
    result = await read_url("https://example.com/spec.html#section-2", max_chars=300)
    note, body = result.text.split("\n\n", 1)
    assert note == "（链接里的 #section-2 指向「2. Timestamps」，下面从那里读起；前面的内容请读不带 # 的链接）"
    assert "第二节正文第 0 段" in body[:60]
    assert "第一节" not in body
    plain = await read_url("https://example.com/spec.html", max_chars=300)
    assert plain.text.startswith(("第一节正文第 0 段", "## 1. Background"))


@pytest.mark.parametrize(
    ("fragment", "says"),
    [
        ("category=code_v3&dataset=code_v3|2026-10|0", "是网页脚本用的状态参数"),
        ("no-such-anchor", "在页面里没找到对应位置"),
    ],
)
async def test_unresolved_fragment_is_explained(serve: Callable[[Handler], None], fragment: str, says: str) -> None:
    serve(lambda r: httpx.Response(200, html=SPEC))
    result = await read_url(f"https://example.com/spec.html#{fragment}", max_chars=300)
    note, body = result.text.split("\n\n", 1)
    assert note.startswith(f"（链接里的 #{fragment} {says}")
    assert "第一节正文第 0 段" in body


@pytest.mark.parametrize("fragment", ["a%00b", "a%EF%BF%BEb"])  # a control character, U+FFFE: not XML strings
async def test_fragment_that_no_element_can_have_still_reads_the_page(
    serve: Callable[[Handler], None], fragment: str
) -> None:
    serve(lambda r: httpx.Response(200, html=SPEC))
    note, body = (await read_url(f"https://example.com/spec.html#{fragment}", max_chars=300)).text.split("\n\n", 1)
    assert note.endswith("在页面里没找到对应位置，下面从页面开头读起）")
    assert "第一节正文第 0 段" in body


async def test_text_fragment_is_not_called_script_state(serve: Callable[[Handler], None]) -> None:
    serve(lambda r: httpx.Response(200, html=SPEC))
    plain = await read_url("https://example.com/spec.html", max_chars=300)
    highlight = await read_url("https://example.com/spec.html#:~:text=第二节正文第%203%20段,编码方式", max_chars=300)
    assert highlight.text == plain.text
    both = await read_url("https://example.com/spec.html#section-2:~:text=nothing", max_chars=300)
    assert both.text.startswith("（链接里的 #section-2 指向「2. Timestamps」")


async def test_fragment_matches_a_markdown_heading_without_page_source(adapter_text: Callable[[str], None]) -> None:
    readme = "\n\n".join(["# Widget", *[f"Intro paragraph {i} about the widget." for i in range(10)]])
    adapter_text(readme + "\n\n## Getting Started\n\nRun `widget init` first.\n\n## License\n\nMIT")
    result = await read_url("https://example.com/acme/widget#getting-started", max_chars=200)
    note, body = result.text.split("\n\n", 1)
    assert note.startswith("（链接里的 #getting-started 指向「Getting Started」")
    assert body.startswith("## Getting Started\n\nRun `widget init` first.")


async def test_crafted_markdown_headings_do_not_stall_the_lookup(adapter_text: Callable[[str], None]) -> None:
    crafted = ["# a" + " " * 200_000 + "b", "## a" + "#" * 200_000 + "b", "# " + "[" * 200_000]
    adapter_text("\n\n".join([*crafted, "## Usage", "Run it."]))
    started = time.perf_counter()
    result = await read_url("https://example.com/acme/widget#usage", max_chars=1000)
    assert time.perf_counter() - started < 1
    assert result.text == (
        "（链接里的 #usage 指向「Usage」，下面从那里读起；前面的内容请读不带 # 的链接）\n\n## Usage\n\nRun it."
    )


# --------------------------------------------------------------------------- Statuspage

STATUSPAGE_INCIDENT = {
    "page": {"id": "p1", "name": "Example Cloud", "url": "https://status.example.com", "time_zone": "Etc/UTC"},
    "incident": {
        "id": "abcd1234efgh",
        "name": "Elevated API errors",
        "status": "resolved",
        "impact": "major",
        "created_at": "2026-01-02T03:04:05.000Z",
        "started_at": "2026-01-02T03:04:05.000Z",
        "resolved_at": "2026-01-02T05:06:07.000Z",
        "incident_updates": [  # newest first, as the API lists them
            {
                "status": "resolved",
                "body": "This incident has been resolved.",
                "display_at": "2026-01-02T05:06:07.000Z",
                "affected_components": [
                    {"code": "c1", "name": "API", "old_status": "partial_outage", "new_status": "operational"}
                ],
            },
            {
                "status": "investigating",
                "body": "We are investigating elevated error rates.",
                "display_at": "2026-01-02T03:04:05.000Z",
                "affected_components": [
                    {"code": "c1", "name": "API", "old_status": "operational", "new_status": "partial_outage"},
                    {"code": "c2", "name": "Dashboard", "old_status": "operational", "new_status": "operational"},
                ],
            },
        ],
        "components": [{"id": "c1", "name": "API", "status": "operational"}, {"id": "c2", "name": "Dashboard"}],
    },
}


async def test_statuspage_incident_uses_the_public_api_on_any_host() -> None:
    url = httpx.URL("https://status.example.com/incidents/abcd1234efgh")
    assert web._adapter_for(url) is web._statuspage_incident
    assert web._adapter_for(httpx.URL("https://status.example.com/history")) is None
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        if request.url.path == "/api/v2/incidents/abcd1234efgh.json":
            return httpx.Response(200, json=STATUSPAGE_INCIDENT)
        postmortem = {"postmortem_body": "## Root cause\n\nA bad deploy.", "postmortem_published_at": "2026-01-03"}
        return httpx.Response(200, json=postmortem)

    async with _client(handler) as client:
        result = await web._statuspage_incident(client, url)
    assert isinstance(result, Extracted)
    assert (result.title, result.via, result.final_url) == ("Elevated API errors", "statuspage", str(url))
    text = result.text
    assert text.startswith("状态页：Example Cloud\n状态：已解决 · 影响：严重\n")
    assert "时间：开始 2026-01-02 03:04 UTC · 解决 2026-01-02 05:06 UTC" in text
    assert "受影响组件：API、Dashboard" in text
    first = text.index("2026-01-02 03:04 UTC · 调查中（API 正常→部分中断）\nWe are investigating elevated error rates.")
    assert first < text.index("2026-01-02 05:06 UTC · 已解决（API 部分中断→正常）\nThis incident has been resolved.")
    assert text.endswith("事后分析（Postmortem）：\n\n## Root cause\n\nA bad deploy.")
    assert seen == ["/api/v2/incidents/abcd1234efgh.json", "/incidents/abcd1234efgh.json"]


async def test_statuspage_path_on_other_sites_falls_back_to_generic() -> None:
    async with _client(lambda r: httpx.Response(404, html="<html>Not found</html>")) as client:
        assert await web._statuspage_incident(client, httpx.URL("https://example.com/incidents/abcd1234efgh")) is None


async def test_statuspage_reads_at_most_2_mb_and_survives_bad_json() -> None:
    pulled = 0

    async def huge() -> AsyncIterator[bytes]:
        nonlocal pulled
        for _ in range(64):
            pulled += 1
            yield b"[" * (1024 * 1024)

    url = httpx.URL("https://status.example.net/incidents/abcd1234efgh")
    async with _client(lambda r: httpx.Response(200, content=huge())) as client:
        assert await web._statuspage_incident(client, url) is None
    assert pulled == 3  # past the 2 MiB cap after 3 MiB: the other 61 MiB are never read
    async with _client(lambda r: httpx.Response(200, content=b"[" * 100_000)) as client:
        assert await web._statuspage_incident(client, url) is None  # nested too deep for json.loads


async def test_stalled_adapter_or_download_times_out(
    serve: Callable[[Handler], None], monkeypatch: pytest.MonkeyPatch
) -> None:
    async def stalled(client: httpx.AsyncClient, url: httpx.URL) -> web.AdapterResult:
        await asyncio.sleep(60)

    @asynccontextmanager
    async def trickling(url: str, **_: object) -> AsyncGenerator[GuardedResponse]:
        async def chunks() -> AsyncIterator[bytes]:
            while True:
                await asyncio.sleep(0.01)
                yield b"x"

        yield GuardedResponse(url, 200, {"content-type": "text/html"}, None, chunks())

    monkeypatch.setattr(web, "_adapter_for", lambda url: stalled if url.path.startswith("/incidents/") else None)
    monkeypatch.setattr(web, "impersonated_stream", trickling)
    started = time.perf_counter()
    for url in ("https://status.example.com/incidents/abcd1234efgh", "https://example.com/slow"):
        with pytest.raises(web.Unreadable, match="网页加载超时"):
            await read_url(url, timeout=0.05)
    assert time.perf_counter() - started < 5


# --------------------------------------------------------------------------- Ubuntu CVE

UBUNTU_CVE = {
    "id": "CVE-2026-0001",
    "published": "2026-01-15T00:00:00",
    "updated_at": "2026-01-20T10:00:00+00:00",
    "priority": "high",
    "cvss3": 9.8,
    "impact": {
        "baseMetricV3": {
            "cvssV3": {
                "baseScore": 9.8,
                "baseSeverity": "CRITICAL",
                "vectorString": "CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H",
                "version": "3.1",
            }
        }
    },
    "notes": [
        {"author": "", "note": "Priority reason:\nRemote code execution"},
        {"author": "sectriage", "note": "Only builds with frob-net enabled are affected."},
    ],
    "description": "\nA use-after-free in the frobnicator allows remote code execution.",
    "ubuntu_description": "",
    "mitigation": "",
    "notices_ids": ["USN-9999-1"],
    "packages": [
        {
            "name": "frob",
            "statuses": [
                {"release_codename": "jammy", "status": "needed", "description": ""},
                {"release_codename": "noble", "status": "released", "description": "1.2-3ubuntu0.1"},
                {"release_codename": "upstream", "status": "released", "description": "1.3"},
                {"release_codename": "bionic", "status": "DNE", "description": ""},
                {"release_codename": "xenial", "status": "ignored", "description": "end of standard support"},
            ],
        }
    ],
    "references": ["https://www.cve.org/CVERecord?id=CVE-2026-0001"],
}
UBUNTU_RELEASES = {
    "releases": [
        {"codename": "upstream", "version": None, "lts": None},
        {"codename": "noble", "version": "24.04", "lts": True},
        {"codename": "jammy", "version": "22.04", "lts": True},
        {"codename": "bionic", "version": "18.04", "lts": True},
        {"codename": "xenial", "version": "16.04", "lts": True},
    ]
}


async def test_ubuntu_cve_shows_priority_and_cvss_values() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/security/cves/CVE-2026-0001.json":
            return httpx.Response(200, json=UBUNTU_CVE)
        assert request.url.path == "/security/releases.json"
        return httpx.Response(200, json=UBUNTU_RELEASES)

    async with _client(handler) as client:
        result = await web._ubuntu_cve(client, httpx.URL("https://ubuntu.com/security/CVE-2026-0001"))
        assert await web._ubuntu_cve(client, httpx.URL("https://ubuntu.com/security/notices")) is None
    assert isinstance(result, Extracted)
    assert (result.title, result.via) == ("CVE-2026-0001 | Ubuntu", "ubuntu")
    text = result.text
    assert text.startswith(
        "发布 2026-01-15 · 更新 2026-01-20\n"
        "Ubuntu 优先级：High\n"
        "优先级理由：Remote code execution\n"
        "CVSS 3.1 评分：9.8 Critical（CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H）\n"
    )
    assert "描述：\nA use-after-free in the frobnicator allows remote code execution." in text
    assert "备注：\nsectriage：Only builds with frob-net enabled are affected." in text
    assert "Ubuntu 安全通告：USN-9999-1" in text
    assert (
        "frob：upstream 已修复（1.3）；24.04 LTS noble 已修复（1.2-3ubuntu0.1）；22.04 LTS jammy 受影响，尚无修复；"
        "16.04 LTS xenial 不修复（end of standard support）"
    ) in text
    assert "bionic" not in text  # DNE: the package is not in that release


# --------------------------------------------------------------------------- Hugging Face

HF_MODEL = {
    "id": "acme/tiny-gguf",
    "pipeline_tag": "text-generation",
    "library_name": "gguf",
    "downloads": 123456,
    "likes": 42,
    "lastModified": "2026-01-05T00:00:00.000Z",
    "cardData": {"license": "apache-2.0", "base_model": "acme/tiny", "base_model_relation": "quantized"},
    "gguf": {"architecture": "llama", "context_length": 32768},
    "siblings": [{"rfilename": "README.md"}, {"rfilename": "tiny-Q4_K_M.gguf"}],
    "widgetData": [{"text": "Hi, what can you help me with?"}],
}
HF_README = "---\nlicense: apache-2.0\nbase_model: acme/tiny\n---\n\n# Tiny GGUF\n\nRuns at 200 tok/s on one GPU.\n"


async def test_hugging_face_model_gives_the_card_not_the_widgets() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if request.url.path == "/api/models/acme/tiny-gguf":
            return httpx.Response(200, json=HF_MODEL)
        if request.url.path == "/acme/tiny-gguf/raw/main/README.md":
            return httpx.Response(200, text=HF_README)
        return httpx.Response(404)

    async with _client(handler) as client:
        result = await web._hugging_face(client, httpx.URL("https://huggingface.co/acme/tiny-gguf"))
        for other in ("https://huggingface.co/spaces/acme/demo", "https://huggingface.co/acme"):
            assert await web._hugging_face(client, httpx.URL(other)) is None
    assert isinstance(result, Extracted)
    assert (result.title, result.via) == ("acme/tiny-gguf · Hugging Face", "huggingface")
    assert result.text == (
        "模型：acme/tiny-gguf\n"
        "任务 text-generation · 库 gguf · GGUF 架构 llama · 上下文长度 32768\n"
        "许可证：apache-2.0\n"
        "基础模型：acme/tiny（quantized）\n"
        "近 30 天下载 12.3万 · 点赞 42 · 更新于 2026-01-05\n"
        "文件（2 个）：README.md、tiny-Q4_K_M.gguf\n"
        "\n"
        "模型卡（README）：\n"
        "\n"
        "# Tiny GGUF\n\nRuns at 200 tok/s on one GPU."
    )
    assert len(seen) == 2  # the API and the README; the page is not needed


async def test_hugging_face_gated_card_comes_from_the_page() -> None:
    page = (
        "<html><body><div class='widgets'>Use this model · Instructions to use acme/corpus with libraries</div>"
        "<div class='model-card-content prose'><h2>Dataset Summary</h2><p>Ten million example sentences.</p></div>"
        "</body></html>"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/datasets/acme/corpus":
            return httpx.Response(200, json={"id": "acme/corpus", "gated": "manual", "likes": 3})
        if request.url.path == "/datasets/acme/corpus/raw/main/README.md":
            return httpx.Response(401, text="Access to dataset acme/corpus is restricted.")
        assert request.url.path == "/datasets/acme/corpus"
        return httpx.Response(200, html=page)

    async with _client(handler) as client:
        result = await web._hugging_face(client, httpx.URL("https://huggingface.co/datasets/acme/corpus"))
    assert isinstance(result, Extracted)
    assert result.final_url == "https://huggingface.co/datasets/acme/corpus"
    assert "（受限仓库：要登录并同意作者的条款才能下载文件）" in result.text
    assert result.text.endswith("数据集卡（README）：\n\n## Dataset Summary\n\nTen million example sentences.")
    assert "Use this model" not in result.text


# --------------------------------------------------------------------------- MSN

MSN_ARTICLE = {
    "title": "Storm closes the harbour",
    "abstract": "A storm closed the harbour.",
    "body": (
        '<div><img data-reference="image" data-document-id="cms/api/amp/image/AA1img"></div>'
        "<p>A storm closed the harbour on Monday.</p><p>Ferries resume on Wednesday.</p>"
    ),
    "provider": {"name": "Example News"},
    "authors": [{"name": "Jane Doe"}],
    "publishedDateTime": "2026-01-02T03:04:05Z",
    "sourceHref": "https://news.example.com/storm",
}
# What the browser rendered for an MSN article in production (the IAB consent dialog), trimmed.
MSN_CONSENT = (
    "<html><head><title>Storm closes the harbour</title></head><body><div>"
    "<p>Microsoft and our third-party vendors use cookies to store and access information such as unique IDs and "
    "personal data/personal information to deliver, maintain, and improve our services and ads. If you agree, MSN "
    "and Microsoft Bing will personalise the content and ads that you see. You can select 'I Accept' to consent to "
    "these uses or click on 'Manage preferences' to review your options.</p>"
    "<p>Privacy statement Number of Partners (vendors): 1019.</p><p>We and our partners process data to</p>"
    "<p>Use precise geolocation data and identify devices based on information actively requested.</p>"
    "</div></body></html>"
)


async def test_msn_article_comes_from_the_content_api() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if request.url.path.endswith("/AA1abcDE"):
            return httpx.Response(200, json=MSN_ARTICLE)
        return httpx.Response(410, json=[])

    async with _client(handler) as client:
        url = "https://www.msn.com/en-in/news/other/storm-closes-the-harbour/ar-AA1abcDE?ocid=share"
        result = await web._msn_article(client, httpx.URL(url))
        with pytest.raises(ToolError, match="下架"):
            await web._msn_article(client, httpx.URL("https://www.msn.com/en-us/news/world/gone/ar-AA0gone0"))
        assert await web._msn_article(client, httpx.URL("https://www.msn.com/en-us/feed")) is None
    assert seen[0] == "https://assets.msn.com/content/view/v2/Detail/en-in/AA1abcDE"
    assert isinstance(result, Extracted)
    assert (result.title, result.via) == ("Storm closes the harbour", "msn")
    assert result.text == (
        "来源：Example News · 作者：Jane Doe\n"
        "发布时间：2026-01-02 11:04\n"
        "原文：https://news.example.com/storm\n"
        "\n"
        "A storm closed the harbour on Monday.\n\nFerries resume on Wednesday."
    )


async def test_msn_consent_wall_fails_with_the_reason(serve: Callable[[Handler], None]) -> None:
    serve(lambda r: httpx.Response(200, html=MSN_CONSENT))
    with pytest.raises(web.Unreadable, match="Cookie 同意弹窗"):
        await web._fetch_generic("https://www.msn.com/en-in/news/other/storm/ar-AA1abcDE", None, 5)
