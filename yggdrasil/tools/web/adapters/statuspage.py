"""Atlassian Statuspage: incidents on any status page's own domain, from its public API."""

import json
import re
from datetime import UTC, datetime
from typing import Any

import httpx

from yggdrasil.core.log import LOGGER
from yggdrasil.tools.errors import ToolError
from yggdrasil.tools.web.model import MB, AdapterResult, Extracted, read_capped

# Atlassian Statuspage incidents, served on each status page's own domain (status.example.com/incidents/<id>).
INCIDENT_PATH_RE = re.compile(r"^/incidents/([a-z0-9]{12})/?$")
_STATUSPAGE_LABELS = {
    "investigating": "调查中",
    "identified": "已找到原因",
    "monitoring": "观察中",
    "resolved": "已解决",
    "postmortem": "事后分析",
    "scheduled": "已排期",
    "in_progress": "进行中",
    "verifying": "验证中",
    "completed": "已完成",
    "none": "无",
    "minor": "轻微",
    "major": "严重",
    "critical": "重大",
    "maintenance": "维护",
    "operational": "正常",
    "degraded_performance": "性能下降",
    "partial_outage": "部分中断",
    "major_outage": "大面积中断",
    "under_maintenance": "维护中",
}


async def incident(client: httpx.AsyncClient, url: httpx.URL) -> AdapterResult:
    """An incident from the status page's public API: without scripts the page shows only its subscribe dialog.
    Other sites with such paths answer the API with an error and take the generic path."""
    match = INCIDENT_PATH_RE.match(url.path)
    if match is None:
        return None
    incident_id = match.group(1)
    api = url.copy_with(path=f"/api/v2/incidents/{incident_id}.json", query=None)
    data = await _statuspage_json(client, api)
    incident = data.get("incident") if isinstance(data, dict) else None
    if not isinstance(incident, dict) or not incident.get("name"):
        return None
    postmortem = ""
    if incident.get("status") in ("resolved", "postmortem"):
        # Only the page's own JSON, not the v2 API, carries the postmortem.
        own = url.copy_with(path=f"/incidents/{incident_id}.json", query=None)
        full = await _statuspage_json(client, own)
        if isinstance(full, dict) and full.get("postmortem_published_at"):
            postmortem = str(full.get("postmortem_body") or "").strip()
    page = data.get("page")
    return Extracted(
        final_url=str(url.copy_with(path=f"/incidents/{incident_id}", query=None)),
        title=str(incident["name"]),
        text=format_statuspage_incident(page if isinstance(page, dict) else {}, incident, postmortem),
        via="statuspage",
    )


# Incident JSON is a few KB. This adapter runs on any host, so it reads no more than this.
_STATUSPAGE_MAX_BYTES = 2 * MB


async def _statuspage_json(client: httpx.AsyncClient, url: httpx.URL) -> Any:
    """``url`` decoded as JSON, reading at most :data:`_STATUSPAGE_MAX_BYTES`; ``None`` unless that is a whole
    JSON answer with status 200."""
    try:
        async with client.stream("GET", url, headers={"Accept": "application/json"}) as resp:
            if resp.status_code != 200:
                return None
            data, capped = await read_capped(resp.aiter_bytes(), _STATUSPAGE_MAX_BYTES)
    except (httpx.HTTPError, ToolError) as exc:
        LOGGER.warning("statuspage request failed: {!r}", exc)
        return None
    if capped:
        return None
    try:
        return json.loads(data)
    except (ValueError, RecursionError):  # not JSON, or nested too deep to decode
        return None


def format_statuspage_incident(page: dict, incident: dict, postmortem: str) -> str:
    """Status, impact, components, then the updates oldest first; times in UTC and labelled so."""
    lines = [f"状态页：{page['name']}"] if page.get("name") else []
    lines.append(f"状态：{_sp_label(incident.get('status'))} · 影响：{_sp_label(incident.get('impact'))}")
    span = [f"开始 {_utc(started)}"] if (started := incident.get("started_at") or incident.get("created_at")) else []
    if resolved := incident.get("resolved_at"):
        span.append(f"解决 {_utc(resolved)}")
    if span:
        lines.append("时间：" + " · ".join(span))
    if scheduled := incident.get("scheduled_for"):
        until = incident.get("scheduled_until")
        lines.append(f"计划维护：{_utc(scheduled)}" + (f" 至 {_utc(until)}" if until else ""))
    components = [str(c["name"]) for c in incident.get("components") or [] if isinstance(c, dict) and c.get("name")]
    lines.append("受影响组件：" + ("、".join(components) if components else "未标注"))
    updates = [u for u in incident.get("incident_updates") or [] if isinstance(u, dict)]
    updates.sort(key=lambda u: str(u.get("display_at") or u.get("created_at") or ""))
    if updates:
        lines += ["", f"更新记录（共 {len(updates)} 条，按时间先后）："]
    for update in updates:
        head = f"{_utc(update.get('display_at') or update.get('created_at'))} · {_sp_label(update.get('status'))}"
        changes = [
            f"{c.get('name')} {_sp_label(c.get('old_status'))}→{_sp_label(c.get('new_status'))}"
            for c in update.get("affected_components") or []
            if isinstance(c, dict) and c.get("old_status") != c.get("new_status")
        ]
        if changes:
            head += "（" + "；".join(changes) + "）"
        lines += ["", head, str(update.get("body") or "").strip()]
    if postmortem:
        lines += ["", "事后分析（Postmortem）：", "", postmortem]
    return "\n".join(lines)


def _sp_label(value: object) -> str:
    return _STATUSPAGE_LABELS.get(str(value), str(value))


def _utc(stamp: object) -> str:
    """An ISO 8601 timestamp as ``YYYY-MM-DD HH:MM UTC``."""
    try:
        return f"{datetime.fromisoformat(str(stamp)).astimezone(UTC):%Y-%m-%d %H:%M} UTC"
    except ValueError:
        return str(stamp)
