"""Workspace records, their Redis encoding, and the pure rules around them (names, renewal, links).

A group has a few named workspaces, one per topic: ``(group_id, name)`` identifies one. Its record is the hash
``ygg:sandbox:ws:<gid>:<name>``; ``ygg:sandbox:wsidx:<gid>`` is the set of the group's names. A record without a
claim is *pending*: reserved by ``open_workspace(new=True)`` (or emptied by a reset), its pod is created on first
use; pending records carry a TTL so an unused reservation frees its slot by itself.
"""

from __future__ import annotations

import json
import re
import secrets
import string
from dataclasses import dataclass, field
from typing import Any

WS_KEY_PREFIX = "ygg:sandbox:ws:"
INDEX_KEY_PREFIX = "ygg:sandbox:wsidx:"
LOCK_KEY_PREFIX = "ygg:sandbox:lock:"
GROUP_LOCK_KEY_PREFIX = "ygg:sandbox:grouplock:"
KEEP_KEY_PREFIX = "ygg:sandbox:keep:"
RESERVED_PORTS = frozenset({8080, 9090})
"""sandboxd's REST and gRPC ports, never exposed."""
NAME_MAX = 32
DESCRIPTION_MAX = 200

_ALNUM = string.ascii_lowercase + string.digits
# A Kubernetes label value as well (the claim's ``yggdrasil/workspace`` label): no trailing '-'.
_NAME = re.compile(rf"[a-z0-9](?:[a-z0-9-]{{0,{NAME_MAX - 2}}}[a-z0-9])?")
_CLAIM_MAX = 63
_CLAIM_SUFFIX = 6


def ws_key(group_id: int, name: str) -> str:
    return f"{WS_KEY_PREFIX}{group_id}:{name}"


def parse_ws_key(key: str) -> tuple[int, str] | None:
    """``(group_id, name)`` of a workspace record key; ``None`` for anything else."""
    gid, sep, name = key.removeprefix(WS_KEY_PREFIX).partition(":")
    if not sep or not valid_name(name):
        return None
    try:
        return int(gid), name
    except ValueError:
        return None


def index_key(group_id: int) -> str:
    """Set of the group's workspace names (members whose record expired are pruned on read)."""
    return f"{INDEX_KEY_PREFIX}{group_id}"


def lock_key(group_id: int, name: str) -> str:
    """Held while attaching to a workspace and during its last release (reap, pending recreate)."""
    return f"{LOCK_KEY_PREFIX}{group_id}:{name}"


def group_lock_key(group_id: int) -> str:
    """Held while reserving a new workspace, so the per-group cap holds across invocations and replicas."""
    return f"{GROUP_LOCK_KEY_PREFIX}{group_id}"


def keep_key(group_id: int, name: str) -> str:
    """Hash of processes the end-of-invocation reap must spare: ``kernel`` -> pid, ``job:<id>`` -> pid."""
    return f"{KEEP_KEY_PREFIX}{group_id}:{name}"


def valid_name(name: str) -> bool:
    return _NAME.fullmatch(name) is not None


def name_error(name: str) -> str | None:
    """The Chinese reason ``name`` cannot name a workspace, or ``None`` when it can."""
    if valid_name(name):
        return None
    return (
        f"工作区名字「{name}」不合规：只能用小写字母、数字和 -，以字母或数字开头和结尾，最长 {NAME_MAX} 个字符"
        "（例如 pelican-game）"
    )


def clean_description(text: str) -> str:
    """One line of at most ``DESCRIPTION_MAX`` characters."""
    return " ".join(text.split())[:DESCRIPTION_MAX]


def random_id(n: int) -> str:
    return "".join(secrets.choice(_ALNUM) for _ in range(n))


def claim_name(group_id: int, name: str) -> str:
    """``ygg-sbx-<gid>-<name>-<random>``, the name cut so the whole fits a 63-character object name."""
    prefix = f"ygg-sbx-{group_id}-"
    room = _CLAIM_MAX - len(prefix) - 1 - _CLAIM_SUFFIX
    trimmed = name[: max(room, 0)].rstrip("-")
    middle = f"{trimmed}-" if trimmed else ""
    return f"{prefix}{middle}{random_id(_CLAIM_SUFFIX)}"


def link_id() -> str:
    return random_id(10)


def link_name(link: str) -> str:
    """Service/IngressRoute name and public subdomain of a link: ``sbx-<id>``."""
    return f"sbx-{link}"


def validate_port(port: int) -> str | None:
    """Return the Chinese reason ``port`` cannot be exposed, or ``None`` when it can."""
    if type(port) is not int or not 1024 <= port <= 65535:
        return "端口必须在 1024–65535 之间"
    if port in RESERVED_PORTS:
        return f"端口 {port} 是沙箱服务自己用的，换一个"
    return None


def shutdown_at(*, now: float, max_until: float, idle_s: float, has_links: bool) -> float:
    """The claim's next ``shutdownTime``: idle timeout from now, or the hard cap ``max_until`` while links are
    live; never past ``max_until``."""
    return max_until if has_links else min(now + idle_s, max_until)


@dataclass(frozen=True)
class Link:
    port: int
    url: str
    command: str
    created_at: float
    pid: int | None = None


@dataclass(frozen=True)
class WorkspaceInfo:
    group_id: int
    name: str
    description: str
    claim: str  # "" while pending
    sandbox: str | None
    pod_ip: str
    created_at: float
    last_used: float
    expires_at: float
    max_until: float
    active: int
    recreate: bool
    links: tuple[Link, ...]
    jobs: tuple[str, ...] = ()
    """Ids of the background jobs running in it (its keep hash)."""

    @property
    def pending(self) -> bool:
        """Reserved or reset, no pod yet: created on first use."""
        return not self.claim


@dataclass(frozen=True)
class LinkRecord:
    """A link as stored: the public fields plus its Kubernetes object id and the listener's process (pid and
    process group, both spared by the end-of-invocation reap)."""

    id: str
    port: int
    url: str
    command: str
    created_at: float
    pgid: int | None
    pid: int | None = None

    def public(self) -> Link:
        return Link(port=self.port, url=self.url, command=self.command, created_at=self.created_at, pid=self.pid)

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "port": self.port,
            "url": self.url,
            "command": self.command,
            "created_at": self.created_at,
            "pgid": self.pgid,
            "pid": self.pid,
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> LinkRecord:
        pgid, pid = data.get("pgid"), data.get("pid")
        return cls(
            id=str(data["id"]),
            port=int(data["port"]),
            url=str(data["url"]),
            command=str(data.get("command", "")),
            created_at=float(data.get("created_at", 0.0)),
            pgid=int(pgid) if pgid is not None else None,
            pid=int(pid) if pid is not None else None,
        )


@dataclass
class Mapping:
    """``ygg:sandbox:ws:<gid>:<name>`` as a Redis hash; ``active`` is changed only with HINCRBY. ``claim`` is ""
    while the workspace is pending. ``max_until`` is the hard cap on the claim's lifetime: creation plus the max
    age, pushed out when a background job starts in it."""

    group_id: int
    name: str
    description: str
    claim: str
    sandbox: str | None
    pod_ip: str
    created_at: float
    last_used: float
    expires_at: float
    max_until: float
    active: int = 0
    recreate: bool = False
    links: list[LinkRecord] = field(default_factory=list)

    @property
    def pending(self) -> bool:
        return not self.claim

    def to_hash(self) -> dict[str, str]:
        return {
            "name": self.name,
            "description": self.description,
            "claim": self.claim,
            "sandbox": self.sandbox or "",
            "pod_ip": self.pod_ip,
            "created_at": repr(self.created_at),
            "last_used": repr(self.last_used),
            "expires_at": repr(self.expires_at),
            "max_until": repr(self.max_until),
            "active": str(self.active),
            "recreate": "1" if self.recreate else "0",
            "links": links_json(self.links),
        }

    @classmethod
    def from_hash(cls, group_id: int, data: dict[str, str]) -> Mapping | None:
        """Decode a stored hash; ``None`` for an empty or unusable one (e.g. only a stray ``active`` counter)."""
        name = data.get("name", "")
        if not valid_name(name) or "claim" not in data:
            return None
        try:
            links = [LinkRecord.from_json(x) for x in json.loads(data.get("links") or "[]")]
            return cls(
                group_id=group_id,
                name=name,
                description=data.get("description", ""),
                claim=data["claim"],
                sandbox=data.get("sandbox") or None,
                pod_ip=data.get("pod_ip", ""),
                created_at=float(data.get("created_at", 0.0)),
                last_used=float(data.get("last_used", 0.0)),
                expires_at=float(data.get("expires_at", 0.0)),
                max_until=float(data.get("max_until", 0.0)),
                active=max(0, int(data.get("active", "0"))),
                recreate=data.get("recreate") == "1",
                links=links,
            )
        except (ValueError, KeyError, TypeError):
            return None

    def info(self, jobs: tuple[str, ...] = ()) -> WorkspaceInfo:
        return WorkspaceInfo(
            group_id=self.group_id,
            name=self.name,
            description=self.description,
            claim=self.claim,
            sandbox=self.sandbox,
            pod_ip=self.pod_ip,
            created_at=self.created_at,
            last_used=self.last_used,
            expires_at=self.expires_at,
            max_until=self.max_until,
            active=self.active,
            recreate=self.recreate,
            links=tuple(x.public() for x in self.links),
            jobs=jobs,
        )


def links_json(links: list[LinkRecord]) -> str:
    return json.dumps([x.to_json() for x in links], ensure_ascii=False)
