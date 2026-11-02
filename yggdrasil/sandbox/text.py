"""Workspace text for the model: a workspace's file tree (the script that lists it, and its rendering) and the
group's workspace listing."""

from __future__ import annotations

import shlex
from datetime import UTC, datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence

    from yggdrasil.sandbox.state import WorkspaceInfo

_TREE_SKIP = (
    ".local",
    "node_modules",
    ".git",
    ".ygg",
    ".pydantic-ai-harness",
    "__pycache__",
    ".cache",
    ".npm",
    ".npm-global",
    ".config",
    ".ipython",
    ".jupyter",
    ".venv",
    "venv",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".pydantic-ai-conformance-*",
)
_TREE_LINES = 50
TREE_SCRIPT = (
    'cd -- "$1" || exit 1; find . -mindepth 1 \\( '
    + " -o ".join(f"-name {shlex.quote(n)}" for n in _TREE_SKIP)
    + " \\) -prune -o -printf '%y\\t%s\\t%T@\\t%P\\n' | head -n 2000"
)
"""``sh -c`` script listing directory ``$1`` (caches and tool state pruned) in the form ``format_tree`` reads."""


def fmt_size(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} KB"
    return f"{n / 1024 / 1024:.1f} MB"


def format_tree(raw: str, *, limit: int = _TREE_LINES) -> str:
    """Render `find -printf '%y\\t%s\\t%T@\\t%P\\n'` output as at most ``limit`` sorted lines."""
    rows: list[tuple[str, str]] = []
    for line in raw.splitlines():
        parts = line.split("\t", 3)
        if len(parts) != 4 or not parts[3]:
            continue
        kind, size, mtime, path = parts
        try:
            stamp = datetime.fromtimestamp(float(mtime), UTC).astimezone().strftime("%m-%d %H:%M")
        except (ValueError, OSError, OverflowError):
            stamp = "?"
        if kind == "d":
            rows.append((path, f"{path}/"))
        else:
            size_text = fmt_size(int(size)) if size.isdigit() else "?"
            rows.append((path, f"{path}  {size_text}  {stamp}"))
    rows.sort()
    lines = [text for _, text in rows[:limit]]
    if len(rows) > limit:
        lines.append(f"…（还有 {len(rows) - limit} 项没列出）")
    return "\n".join(lines)


def _span(seconds: float) -> str:
    minutes = int(max(0.0, seconds) // 60)
    if minutes < 1:
        return "不到 1 分钟"
    if minutes < 60:
        return f"{minutes} 分钟"
    hours, rest = divmod(minutes, 60)
    return f"{hours} 小时 {rest} 分钟" if rest else f"{hours} 小时"


def format_workspaces(
    infos: Sequence[WorkspaceInfo], *, now: float, titles: dict[str, str], current: str | None = None
) -> str:
    """The group's workspaces for the model: name, description, age, idle time, time left, links, jobs."""
    lines: list[str] = []
    for w in infos:
        mark = "（当前选中）" if w.name == current else ""
        lines.append(f"- {w.name}{mark}：{w.description or '（没有说明）'}")
        if w.pending:
            lines.append("  空的，还没启动（第一次用时创建）")
            continue
        facts = [f"已存在 {_span(now - w.created_at)}", "正在使用" if w.active else f"闲置 {_span(now - w.last_used)}"]
        if w.links or w.jobs:
            facts.append(f"最多还能保留 {_span(w.max_until - now)}")
        else:
            facts.append(f"约 {_span(w.expires_at - now)}后闲置回收")
        if w.links:
            facts.append("公开链接 " + "、".join(f"{x.url}（端口 {x.port}）" for x in w.links))
        if w.jobs:
            facts.append("后台任务 " + "、".join(f"{j}「{titles.get(j, '')}」" for j in w.jobs))
        lines.append("  " + "；".join(facts))
    return "\n".join(lines)
