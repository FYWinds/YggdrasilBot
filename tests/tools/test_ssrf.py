import ipaddress

import httpx
import pytest

from yggdrasil.tools import ssrf
from yggdrasil.tools.errors import ToolError


@pytest.fixture
def fake_dns(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[str]]:
    """Replace DNS with a table; unknown names fail like NXDOMAIN."""
    table: dict[str, list[str]] = {}

    async def resolve(host: str, port: int, dns_timeout: float | None) -> list[ssrf.IPAddress]:
        if host not in table:
            raise ToolError(f"无法解析域名 {host}")
        return [ipaddress.ip_address(a) for a in table[host]]

    monkeypatch.setattr(ssrf, "_resolve", resolve)
    return table


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/",
        "http://10.42.1.5:8080/",
        "http://10.43.0.1/",
        "http://172.16.0.1/",
        "http://192.168.1.1/",
        "http://169.254.169.254/latest/meta-data/",
        "http://100.64.0.1/",
        "http://0.0.0.0/",
        "http://224.0.0.1/",
        "http://240.0.0.1/",
        "http://[::1]/",
        "http://[::]/",
        "http://[::ffff:127.0.0.1]/",
        "http://[::ffff:a9fe:a9fe]/",
        "http://[fe80::1]/",
        "http://[fd00::1]/",
        "http://[ff02::1]/",
        "http://[64:ff9b::a00:1]/",  # NAT64 of 10.0.0.1
        "http://[2002:7f00:1::]/",  # 6to4 of 127.0.0.1
        # inet_aton-style numeric hosts that browsers and libc accept
        "http://2130706433/",
        "http://0x7f000001/",
        "http://0177.0.0.1/",
        "http://127.1/",
        "http://0x7f.1/",
        "http://017700000001/",
        "http://3232235777/",  # 192.168.1.1
        # internal names
        "http://localhost/",
        "http://LOCALHOST./",
        "http://api.localhost/",
        "http://printer.local/",
        "http://redis:6379/",
        "http://ygg-postgres.ygg.svc.cluster.local/",
        "http://metadata.google.internal/",
        # schemes and credentials
        "file:///etc/passwd",
        "ftp://example.com/",
        "gopher://example.com/",
        "http://user:pass@example.com/",
    ],
)
async def test_rejects_non_public_targets_without_dns(url: str, fake_dns: dict[str, list[str]]) -> None:
    with pytest.raises(ToolError):
        await ssrf.check_url(url)


async def test_rejects_name_resolving_to_private(fake_dns: dict[str, list[str]]) -> None:
    fake_dns["evil.example.com"] = ["10.43.0.10"]
    with pytest.raises(ToolError, match="内网"):
        await ssrf.check_url("https://evil.example.com/")


async def test_rejects_when_any_resolved_address_is_private(fake_dns: dict[str, list[str]]) -> None:
    fake_dns["mixed.example.com"] = ["93.184.215.14", "::1"]
    with pytest.raises(ToolError):
        await ssrf.check_url("https://mixed.example.com/")


async def test_unresolvable_name_is_a_tool_error(fake_dns: dict[str, list[str]]) -> None:
    with pytest.raises(ToolError, match="无法解析"):
        await ssrf.check_url("https://nx.example.com/")


async def test_public_url_is_normalized(fake_dns: dict[str, list[str]]) -> None:
    fake_dns["example.com"] = ["93.184.215.14", "2606:2800:21f:cb07:6820:80da:af6b:8b2c"]
    assert await ssrf.check_url("  example.com/a?b=1#frag ") == "https://example.com/a?b=1"
    # Numeric public hosts are rewritten to dotted form so every consumer sees the same address.
    assert await ssrf.check_url("http://134744072/x") == "http://8.8.8.8/x"


async def test_connect_time_resolution_blocks_rebinding(fake_dns: dict[str, list[str]]) -> None:
    fake_dns["rebind.example.com"] = ["127.0.0.1"]
    backend = ssrf._GuardedBackend()
    with pytest.raises(ToolError):
        await backend.connect_tcp("rebind.example.com", 443)


async def test_every_redirect_hop_is_rechecked(monkeypatch: pytest.MonkeyPatch) -> None:
    hops: list[str] = []

    async def upstream(self: httpx.AsyncHTTPTransport, request: httpx.Request) -> httpx.Response:
        hops.append(str(request.url))
        return httpx.Response(302, headers={"Location": "http://169.254.169.254/latest/meta-data/"})

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", upstream)
    async with ssrf.guarded_client() as client:
        with pytest.raises(ToolError):
            await client.get("https://example.com/start")
    assert hops == ["https://example.com/start"]


async def test_redirect_chain_is_capped(monkeypatch: pytest.MonkeyPatch) -> None:
    async def upstream(self: httpx.AsyncHTTPTransport, request: httpx.Request) -> httpx.Response:
        n = int(request.url.params.get("n", "0"))
        return httpx.Response(302, headers={"Location": f"https://example.com/loop?n={n + 1}"})

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", upstream)
    async with ssrf.guarded_client() as client:
        with pytest.raises(httpx.TooManyRedirects):
            await client.get("https://example.com/loop")
