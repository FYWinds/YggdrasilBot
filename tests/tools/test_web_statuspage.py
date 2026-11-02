"""The Statuspage adapter of ``read_url``: incidents on any status page's own domain, from its public API."""

from collections.abc import AsyncIterator

import httpx

from tests.support.routes import Routes
from yggdrasil.tools import web
from yggdrasil.tools.web import read_url

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


async def test_statuspage_incident_on_any_host_comes_from_the_public_api(web_page: Routes) -> None:
    postmortem = {"postmortem_body": "## Root cause\n\nA bad deploy.", "postmortem_published_at": "2026-01-03"}
    web_page.answers.update(
        {
            "https://status.example.com/api/v2/incidents/abcd1234efgh.json": httpx.Response(
                200, json=STATUSPAGE_INCIDENT
            ),
            "https://status.example.com/incidents/abcd1234efgh.json": httpx.Response(200, json=postmortem),
        }
    )
    url = "https://status.example.com/incidents/abcd1234efgh"
    result = await read_url(url)
    assert (result.title, result.via, result.final_url) == ("Elevated API errors", "statuspage", url)
    text = result.text
    assert text.startswith("状态页：Example Cloud\n状态：已解决 · 影响：严重\n")
    assert "时间：开始 2026-01-02 03:04 UTC · 解决 2026-01-02 05:06 UTC" in text
    assert "受影响组件：API、Dashboard" in text
    first = text.index("2026-01-02 03:04 UTC · 调查中（API 正常→部分中断）\nWe are investigating elevated error rates.")
    assert first < text.index("2026-01-02 05:06 UTC · 已解决（API 部分中断→正常）\nThis incident has been resolved.")
    assert text.endswith("事后分析（Postmortem）：\n\n## Root cause\n\nA bad deploy.")
    assert [r.url.path for r in web_page.seen] == [
        "/api/v2/incidents/abcd1234efgh.json",
        "/incidents/abcd1234efgh.json",
    ]

    # Other pages of the status site are plain pages.
    page = "<html><head><title>History</title></head><body><p>No incidents reported this month.</p></body></html>"
    web_page.answers["https://status.example.com/history"] = httpx.Response(200, html=page)
    assert (await read_url("https://status.example.com/history")).via == "http"


async def test_statuspage_path_on_other_sites_falls_back_to_generic() -> None:
    async with Routes({"example.com": httpx.Response(404, html="<html>Not found</html>")}).client() as client:
        assert await web._statuspage_incident(client, httpx.URL("https://example.com/incidents/abcd1234efgh")) is None


async def test_statuspage_reads_at_most_2_mb_and_survives_bad_json() -> None:
    pulled = 0

    async def huge() -> AsyncIterator[bytes]:
        nonlocal pulled
        for _ in range(64):
            pulled += 1
            yield b"[" * (1024 * 1024)

    url = httpx.URL("https://status.example.net/incidents/abcd1234efgh")
    async with Routes({"status.example.net": lambda r: httpx.Response(200, content=huge())}).client() as client:
        assert await web._statuspage_incident(client, url) is None
    assert pulled == 3  # past the 2 MiB cap after 3 MiB: the other 61 MiB are never read
    async with Routes({"status.example.net": httpx.Response(200, content=b"[" * 100_000)}).client() as client:
        assert await web._statuspage_incident(client, url) is None  # nested too deep for json.loads
