"""SSRF guard for fetching user-supplied URLs.

Two layers:

* :func:`check_url` validates scheme / host and resolves DNS, rejecting the URL
  if *any* resolved address is not publicly routable.
* :func:`guarded_client` returns an ``httpx.AsyncClient`` whose transport
  re-validates every request (so every redirect hop) and whose network backend
  resolves, vets and then connects to the vetted IP itself. Pinning the IP at
  connect time closes the DNS-rebinding gap between check and connect; TLS still
  uses the original host name for SNI / certificate verification.

The cluster NetworkPolicy egress rules are the second line of defence.
"""

import asyncio
import ipaddress
import re
import socket
import ssl
from collections.abc import Iterable

import certifi
import httpcore
import httpx

from yggdrasil.tools.errors import ToolError

type IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address

MAX_REDIRECTS = 5
_DNS_TIMEOUT = 5.0

# ``is_global`` already excludes RFC 1918, loopback, link-local, CGNAT, documentation
# and reserved space; the explicit lists are belt and braces against stdlib table changes.
_BLOCKED_V4 = tuple(
    ipaddress.IPv4Network(n)
    for n in (
        "0.0.0.0/8",
        "10.0.0.0/8",  # includes the k3s pod (10.42/16) and service (10.43/16) CIDRs
        "100.64.0.0/10",
        "127.0.0.0/8",
        "169.254.0.0/16",
        "172.16.0.0/12",
        "192.0.0.0/24",
        "192.0.2.0/24",
        "192.88.99.0/24",
        "192.168.0.0/16",
        "198.18.0.0/15",
        "198.51.100.0/24",
        "203.0.113.0/24",
        "224.0.0.0/4",
        "240.0.0.0/4",
    )
)
_BLOCKED_V6 = tuple(
    ipaddress.IPv6Network(n)
    for n in (
        "::/96",  # unspecified, loopback and deprecated IPv4-compatible addresses
        "64:ff9b:1::/48",
        "100::/64",
        "2001::/23",
        "2001:db8::/32",
        "fc00::/7",
        "fe80::/10",
        "fec0::/10",
        "ff00::/8",
    )
)
_NAT64 = ipaddress.IPv6Network("64:ff9b::/96")

_BLOCKED_SUFFIXES = (
    ".localhost",
    ".local",
    ".localdomain",
    ".internal",
    ".intranet",
    ".lan",
    ".home.arpa",
    ".svc",
    ".cluster.local",
)
_SCHEME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*://")
_HEX_PART = re.compile(r"0[xX][0-9a-fA-F]*")
_OCT_PART = re.compile(r"0[0-7]*")
_DEC_PART = re.compile(r"[1-9][0-9]*")


def is_public_address(addr: IPAddress) -> bool:
    """Whether ``addr`` is globally routable (embedded IPv4 in IPv6 forms is unwrapped)."""
    if isinstance(addr, ipaddress.IPv6Address):
        if addr.ipv4_mapped is not None:
            return is_public_address(addr.ipv4_mapped)
        if addr.sixtofour is not None:
            return is_public_address(addr.sixtofour)
        if addr in _NAT64:
            return is_public_address(ipaddress.IPv4Address(int(addr) & 0xFFFFFFFF))
        if any(addr in net for net in _BLOCKED_V6):
            return False
    elif any(addr in net for net in _BLOCKED_V4):
        return False
    return addr.is_global and not addr.is_multicast


def _legacy_ipv4(host: str) -> ipaddress.IPv4Address | None:
    """Parse inet_aton-style numeric hosts (``2130706433``, ``0x7f.1``, ``0177.0.0.1``) like browsers do."""
    parts = host.removesuffix(".").split(".")
    if not 1 <= len(parts) <= 4:
        return None
    nums: list[int] = []
    for part in parts:
        if _HEX_PART.fullmatch(part):
            nums.append(int(part[2:] or "0", 16))
        elif _OCT_PART.fullmatch(part):
            nums.append(int(part, 8))
        elif _DEC_PART.fullmatch(part):
            nums.append(int(part))
        else:
            return None
    *head, last = nums
    if any(n > 0xFF for n in head) or last >= 1 << (8 * (4 - len(head))):
        return None
    value = last
    for i, n in enumerate(head):
        value |= n << (8 * (3 - i))
    return ipaddress.IPv4Address(value)


def _literal_ip(host: str) -> IPAddress | None:
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        return _legacy_ipv4(host)


def _parse(url: str) -> httpx.URL:
    url = url.strip()
    if not _SCHEME_RE.match(url):
        url = "https://" + url
    try:
        parsed = httpx.URL(url)
    except httpx.InvalidURL as exc:
        raise ToolError("链接格式不正确") from exc
    return parsed


def _vet_static(parsed: httpx.URL) -> tuple[httpx.URL, str, IPAddress | None]:
    """Checks that need no DNS. Returns (normalized URL, ascii host, literal IP or None)."""
    if parsed.scheme not in ("http", "https"):
        raise ToolError("只支持 http/https 链接")
    if parsed.userinfo:
        raise ToolError("链接中不能包含账号密码")
    host = parsed.raw_host.decode("ascii").lower().removesuffix(".")
    if not host:
        raise ToolError("链接缺少域名")
    ip = _literal_ip(host)
    if ip is not None:
        if not is_public_address(ip):
            raise ToolError("不允许访问内网或保留地址")
        if host != ip.compressed:
            parsed = parsed.copy_with(host=ip.compressed)
        return parsed.copy_with(fragment=None), ip.compressed, ip
    if host == "localhost" or host.endswith(_BLOCKED_SUFFIXES) or "." not in host:
        raise ToolError("不允许访问内网或保留地址")
    return parsed.copy_with(fragment=None), host, None


async def resolve_public(host: str, port: int, *, dns_timeout: float | None = _DNS_TIMEOUT) -> list[IPAddress]:
    """Resolve ``host`` and return its addresses; raise if any of them is not public."""
    ip = _literal_ip(host.strip("[]"))
    if ip is not None:
        addrs = [ip]
    else:
        addrs = await _resolve(host, port, dns_timeout)
    if not addrs:
        raise ToolError(f"无法解析域名 {host}")
    if not all(is_public_address(a) for a in addrs):
        raise ToolError("不允许访问内网或保留地址")
    return addrs


async def _resolve(host: str, port: int, dns_timeout: float | None) -> list[IPAddress]:
    loop = asyncio.get_running_loop()
    try:
        async with asyncio.timeout(dns_timeout):
            infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except (OSError, TimeoutError, UnicodeError) as exc:
        raise ToolError(f"无法解析域名 {host}") from exc
    return _unique(ipaddress.ip_address(str(info[4][0]).split("%", 1)[0]) for info in infos)


def _unique(addrs: Iterable[IPAddress]) -> list[IPAddress]:
    return list(dict.fromkeys(addrs))


async def check_url(url: str) -> str:
    """Validate a user-supplied URL and return it normalized (fragment dropped, numeric hosts canonical).

    Raises :class:`ToolError` for non-http(s) schemes, credentials, internal host names and
    hosts resolving to any non-public address.
    """
    parsed, host, ip = _vet_static(_parse(url))
    if ip is None:
        await resolve_public(host, parsed.port or (443 if parsed.scheme == "https" else 80))
    return str(parsed)


class _GuardedBackend(httpcore.AsyncNetworkBackend):
    """Resolves and vets the host at connect time, then connects to the vetted IP."""

    def __init__(self) -> None:
        self._inner: httpcore.AsyncNetworkBackend = httpcore.AnyIOBackend()  # pyright: ignore[reportAttributeAccessIssue]

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,  # noqa: ASYNC109 - httpcore backend interface
        local_address: str | None = None,
        socket_options: Iterable[httpcore.SOCKET_OPTION] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        addrs = await resolve_public(host, port, dns_timeout=timeout or _DNS_TIMEOUT)
        error: Exception | None = None
        for addr in addrs:
            try:
                return await self._inner.connect_tcp(
                    str(addr), port, timeout=timeout, local_address=local_address, socket_options=socket_options
                )
            except (httpcore.ConnectError, httpcore.ConnectTimeout) as exc:
                error = exc
        assert error is not None
        raise error

    async def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,  # noqa: ASYNC109 - httpcore backend interface
        socket_options: Iterable[httpcore.SOCKET_OPTION] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        raise ToolError("不允许访问本地套接字")

    async def sleep(self, seconds: float) -> None:
        await self._inner.sleep(seconds)


class GuardedTransport(httpx.AsyncHTTPTransport):
    """httpx transport that vets every request URL and pins vetted IPs at connect time."""

    def __init__(self, *, limits: httpx.Limits | None = None) -> None:
        limits = limits or httpx.Limits(max_connections=20, max_keepalive_connections=5)
        super().__init__(trust_env=False, limits=limits)
        # httpx does not expose ``network_backend``; swap the pool it built for an equivalent guarded one.
        self._pool = httpcore.AsyncConnectionPool(
            ssl_context=ssl.create_default_context(cafile=certifi.where()),
            max_connections=limits.max_connections,
            max_keepalive_connections=limits.max_keepalive_connections,
            keepalive_expiry=limits.keepalive_expiry,
            network_backend=_GuardedBackend(),
        )

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        _vet_static(request.url)
        return await super().handle_async_request(request)


def guarded_client(
    *, timeout: float = 20, headers: dict[str, str] | None = None, follow_redirects: bool = True
) -> httpx.AsyncClient:
    """AsyncClient for untrusted URLs: every hop is SSRF-checked, at most :data:`MAX_REDIRECTS` redirects."""
    return httpx.AsyncClient(
        transport=GuardedTransport(),
        follow_redirects=follow_redirects,
        max_redirects=MAX_REDIRECTS,
        timeout=timeout,
        headers=headers,
        trust_env=False,
    )
