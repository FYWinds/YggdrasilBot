"""The Ubuntu CVE adapter of ``read_url``: the security tracker's JSON API instead of its long status pages."""

import httpx

from tests.support.routes import Routes
from yggdrasil.tools import web
from yggdrasil.tools.web import Extracted

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
    routes = Routes(
        {
            "https://ubuntu.com/security/cves/CVE-2026-0001.json": httpx.Response(200, json=UBUNTU_CVE),
            "https://ubuntu.com/security/releases.json": httpx.Response(200, json=UBUNTU_RELEASES),
        }
    )
    async with routes.client() as client:
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
