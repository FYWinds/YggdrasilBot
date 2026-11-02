"""Ubuntu security tracker: a CVE from its JSON API."""

import asyncio
import re

import httpx

from yggdrasil.tools.web.adapters.common import optional_json
from yggdrasil.tools.web.adapters.registry import site_adapter
from yggdrasil.tools.web.model import AdapterResult, Extracted

_UBUNTU_SECURITY = "https://ubuntu.com/security"
_UBUNTU_CVE_RE = re.compile(r"^/security/(?:cves/)?(cve-\d{4}-\d{4,})(?:\.json)?/?$", re.IGNORECASE)
# A package's status in a release, in the tracker's terms; "DNE" (not in that release) is left out.
_UBUNTU_STATUSES = {
    "released": "已修复",
    "pending": "修复中",
    "needed": "受影响，尚无修复",
    "deferred": "受影响，修复推迟",
    "needs-triage": "待评估",
    "ignored": "不修复",
    "not-affected": "不受影响",
}


@site_adapter("ubuntu.com", "www.ubuntu.com")
async def cve(client: httpx.AsyncClient, url: httpx.URL) -> AdapterResult:
    """A CVE of the Ubuntu security tracker from its JSON API: extraction loses the values of the page's priority
    and CVSS boxes, and the page's status table runs to many pages."""
    match = _UBUNTU_CVE_RE.match(url.path)
    if match is None:
        return None
    cve_id = match.group(1).upper()
    cve, releases = await asyncio.gather(
        optional_json(client.get(f"{_UBUNTU_SECURITY}/cves/{cve_id}.json"), "ubuntu cve api"),
        optional_json(client.get(f"{_UBUNTU_SECURITY}/releases.json"), "ubuntu releases api"),
    )
    if not isinstance(cve, dict) or not cve.get("id"):
        return None
    known = releases.get("releases") if isinstance(releases, dict) else None
    text = format_ubuntu_cve(cve, known if isinstance(known, list) else [])
    return Extracted(f"{_UBUNTU_SECURITY}/{cve_id}", f"{cve_id} | Ubuntu", text, "ubuntu")


def format_ubuntu_cve(cve: dict, releases: list) -> str:
    """Priority and CVSS scores, descriptions and notes, then each package's status per release (``releases``, the
    tracker's release list, gives version numbers and the newest-first order)."""
    stamps = (("发布", "published"), ("更新", "updated_at"))
    dates = [f"{label} {str(cve[key])[:10]}" for label, key in stamps if cve.get(key)]
    lines = [" · ".join(dates)] if dates else []
    lines.append(f"Ubuntu 优先级：{str(cve.get('priority') or '').capitalize() or '未评级'}")
    notes: list[str] = []
    for note in cve.get("notes") or []:
        body = str(note.get("note") or "").strip() if isinstance(note, dict) else ""
        if body.lower().startswith("priority reason:"):
            lines.append("优先级理由：" + " ".join(body.split(":", 1)[1].split()))
        elif body:
            notes.append(f"{note['author']}：{body}" if note.get("author") else body)
    impact = raw if isinstance(raw := cve.get("impact"), dict) else {}
    scores = [
        _cvss_line(metric.get(key))
        for metric, key in ((impact.get("baseMetricV3"), "cvssV3"), (impact.get("baseMetricV4"), "cvssV4"))
        if isinstance(metric, dict)
    ]
    lines += [s for s in scores if s] or ([f"CVSS 3 评分：{cve['cvss3']}"] if cve.get("cvss3") is not None else [])
    for label, key in (("描述", "description"), ("Ubuntu 补充说明", "ubuntu_description"), ("缓解措施", "mitigation")):
        if body := str(cve.get(key) or "").strip():
            lines += ["", f"{label}：", body]
    if notes:
        lines += ["", "备注：", *notes]
    if notices := [str(n) for n in cve.get("notices_ids") or [] if n]:
        lines += ["", "Ubuntu 安全通告：" + "、".join(notices)]
    order = {str(r.get("codename")): i for i, r in enumerate(releases) if isinstance(r, dict)}
    names = {str(r.get("codename")): _ubuntu_release(r) for r in releases if isinstance(r, dict)}
    rows = []
    for package in cve.get("packages") or []:
        statuses = [
            s for s in package.get("statuses") or [] if isinstance(s, dict) and s.get("status") not in (None, "DNE")
        ]
        statuses.sort(key=lambda s: order.get(str(s.get("release_codename")), len(order)))
        cells = []
        for status in statuses:
            codename = str(status.get("release_codename"))
            cell = f"{names.get(codename, codename)} {_UBUNTU_STATUSES.get(status['status'], status['status'])}"
            detail = str(status.get("description") or "").strip()
            cells.append(f"{cell}（{detail}）" if detail else cell)
        if cells:
            rows.append(f"{package.get('name')}：" + "；".join(cells))
    if rows:
        lines += ["", f"各版本状态（{len(rows)} 个软件包，新版本在前）：", *rows]
    if references := [str(r) for r in cve.get("references") or [] if r]:
        lines += ["", "参考链接：", *references]
    return "\n".join(lines)


def _cvss_line(cvss: object) -> str | None:
    if not isinstance(cvss, dict) or cvss.get("baseScore") is None:
        return None
    line = f"CVSS {cvss.get('version') or ''} 评分：{cvss['baseScore']}"
    if severity := str(cvss.get("baseSeverity") or "").capitalize():
        line += f" {severity}"
    return f"{line}（{cvss['vectorString']}）" if cvss.get("vectorString") else line


def _ubuntu_release(release: dict) -> str:
    """``24.04 LTS noble``; just the codename for ``upstream``."""
    codename = str(release.get("codename"))
    if not (version := release.get("version")):
        return codename
    return f"{version} LTS {codename}" if release.get("lts") else f"{version} {codename}"
