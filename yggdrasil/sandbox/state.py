"""Workspace records, their Redis encoding, and the pure rules around them (names, renewal, links)."""

from __future__ import annotations

import json
import secrets
import string
from dataclasses import dataclass, field
from typing import Any

WS_KEY_PREFIX = "ygg:sandbox:ws:"
LOCK_KEY_PREFIX = "ygg:sandbox:lock:"
RESERVED_PORTS = frozenset({8080, 9090})
"""sandboxd's REST and gRPC ports, never exposed."""

_ALNUM = string.ascii_lowercase + string.digits


def ws_key(group_id: int) -> str:
    return f"{WS_KEY_PREFIX}{group_id}"


def lock_key(group_id: int) -> str:
    return f"{LOCK_KEY_PREFIX}{group_id}"


def random_id(n: int) -> str:
    return "".join(secrets.choice(_ALNUM) for _ in range(n))


def claim_name(group_id: int) -> str:
    return f"ygg-sbx-{group_id}-{random_id(6)}"


def link_id() -> str:
    return random_id(10)


def validate_port(port: int) -> str | None:
    """Return the Chinese reason ``port`` cannot be exposed, or ``None`` when it can."""
    if type(port) is not int or not 1024 <= port <= 65535:
        return "端口必须在 1024–65535 之间"
    if port in RESERVED_PORTS:
        return f"端口 {port} 是沙箱服务自己用的，换一个"
    return None


def shutdown_at(*, now: float, created_at: float, idle_s: float, max_age_s: float, has_links: bool) -> float:
    """The claim's next ``shutdownTime``: idle timeout from now (max age while links are live), never past the
    hard max age counted from creation."""
    return min(now + (max_age_s if has_links else idle_s), created_at + max_age_s)


@dataclass(frozen=True)
class Link:
    port: int
    url: str
    command: str
    created_at: float


@dataclass(frozen=True)
class WorkspaceInfo:
    group_id: int
    claim: str
    sandbox: str | None
    pod_ip: str
    created_at: float
    last_used: float
    expires_at: float
    active: int
    recreate: bool
    links: tuple[Link, ...]


@dataclass(frozen=True)
class LinkRecord:
    """A link as stored: the public fields plus its Kubernetes object id and the served process group."""

    id: str
    port: int
    url: str
    command: str
    created_at: float
    pgid: int | None

    def public(self) -> Link:
        return Link(port=self.port, url=self.url, command=self.command, created_at=self.created_at)

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "port": self.port,
            "url": self.url,
            "command": self.command,
            "created_at": self.created_at,
            "pgid": self.pgid,
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> LinkRecord:
        pgid = data.get("pgid")
        return cls(
            id=str(data["id"]),
            port=int(data["port"]),
            url=str(data["url"]),
            command=str(data.get("command", "")),
            created_at=float(data.get("created_at", 0.0)),
            pgid=int(pgid) if pgid is not None else None,
        )


@dataclass
class Mapping:
    """``ygg:sandbox:ws:<gid>`` as a Redis hash; ``active`` is changed only with HINCRBY."""

    group_id: int
    claim: str
    sandbox: str | None
    pod_ip: str
    created_at: float
    last_used: float
    expires_at: float
    active: int = 0
    recreate: bool = False
    links: list[LinkRecord] = field(default_factory=list)

    def to_hash(self) -> dict[str, str]:
        return {
            "claim": self.claim,
            "sandbox": self.sandbox or "",
            "pod_ip": self.pod_ip,
            "created_at": repr(self.created_at),
            "last_used": repr(self.last_used),
            "expires_at": repr(self.expires_at),
            "active": str(self.active),
            "recreate": "1" if self.recreate else "0",
            "links": links_json(self.links),
        }

    @classmethod
    def from_hash(cls, group_id: int, data: dict[str, str]) -> Mapping | None:
        """Decode a stored hash; ``None`` for an empty or unusable one (e.g. only a stray ``active`` counter)."""
        if not data.get("claim"):
            return None
        try:
            links = [LinkRecord.from_json(x) for x in json.loads(data.get("links") or "[]")]
            return cls(
                group_id=group_id,
                claim=data["claim"],
                sandbox=data.get("sandbox") or None,
                pod_ip=data.get("pod_ip", ""),
                created_at=float(data.get("created_at", 0.0)),
                last_used=float(data.get("last_used", 0.0)),
                expires_at=float(data.get("expires_at", 0.0)),
                active=max(0, int(data.get("active", "0"))),
                recreate=data.get("recreate") == "1",
                links=links,
            )
        except (ValueError, KeyError, TypeError):
            return None

    def info(self) -> WorkspaceInfo:
        return WorkspaceInfo(
            group_id=self.group_id,
            claim=self.claim,
            sandbox=self.sandbox,
            pod_ip=self.pod_ip,
            created_at=self.created_at,
            last_used=self.last_used,
            expires_at=self.expires_at,
            active=self.active,
            recreate=self.recreate,
            links=tuple(x.public() for x in self.links),
        )


def links_json(links: list[LinkRecord]) -> str:
    return json.dumps([x.to_json() for x in links], ensure_ascii=False)
