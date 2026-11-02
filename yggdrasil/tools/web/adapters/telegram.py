"""Telegram: public channel posts from their embed widget."""

import re
from datetime import datetime

import httpx

from yggdrasil.tools.errors import ToolError
from yggdrasil.tools.web.adapters.common import CN_TZ, fragment_text
from yggdrasil.tools.web.model import AdapterResult, Extracted

_TME_POST_RE = re.compile(r"^/(?:s/)?([A-Za-z0-9_]{3,32})/(\d+)/?$")


async def post(client: httpx.AsyncClient, url: httpx.URL) -> AdapterResult:
    match = _TME_POST_RE.match(url.path)
    if match is None:
        return None
    channel, post = match.groups()
    resp = await client.get(f"https://t.me/{channel}/{post}", params={"embed": "1", "mode": "tme"})
    if resp.status_code != 200:
        return None
    return parse_telegram_embed(resp.text, f"https://t.me/{channel}/{post}")


def parse_telegram_embed(html: str, final_url: str) -> Extracted | None:
    """Turn a t.me ``?embed=1`` widget page into the post text, or ``None`` if it has nothing to show."""
    from lxml import html as lxml_html

    root = lxml_html.fromstring(html)

    def first_text(xpath: str) -> str:
        nodes = root.xpath(xpath)
        return nodes[0].text_content().strip() if nodes else ""

    if root.xpath("//*[contains(@class, 'tgme_widget_message_error')]"):
        raise ToolError("Telegram 消息不存在，或频道不公开")
    owner = first_text("//*[contains(@class, 'tgme_widget_message_owner_name')]")
    lines = [f"频道：{owner}"] if owner else []
    if stamps := root.xpath("//time[@datetime]/@datetime"):
        lines.append(f"发布时间：{datetime.fromisoformat(stamps[0]).astimezone(CN_TZ):%Y-%m-%d %H:%M}")
    if views := first_text("//*[contains(@class, 'tgme_widget_message_views')]"):
        lines.append(f"浏览：{views}")
    found = False
    if texts := root.xpath("//div[contains(@class, 'js-message_text')]"):
        for emoji in texts[0].xpath(".//i[contains(@class, 'emoji')]"):
            for inner in emoji.findall("b"):
                inner.drop_tag()
            emoji.drop_tag()
        lines += ["", fragment_text(str(lxml_html.tostring(texts[0], encoding="unicode")))]
        found = True
    media = []
    if photos := root.xpath("//*[contains(@class, 'tgme_widget_message_photo_wrap')]"):
        media.append(f"图片 {len(photos)} 张")
    if root.xpath("//*[contains(@class, 'tgme_widget_message_video_player')]"):
        media.append("视频")
    if document := first_text("//*[contains(@class, 'tgme_widget_message_document_title')]"):
        media.append(f"文件「{document}」")
    if media:
        lines += ["", "附件：" + "、".join(media)]
        found = True
    preview_title = first_text("//*[contains(@class, 'link_preview_title')]")
    preview_desc = first_text("//*[contains(@class, 'link_preview_description')]")
    if preview_title or preview_desc:
        site = first_text("//*[contains(@class, 'link_preview_site_name')]")
        lines += ["", f"链接预览（{site}）：" if site else "链接预览：", *filter(None, (preview_title, preview_desc))]
        found = True
    if not found:
        return None
    return Extracted(final_url, f"{owner} 的 Telegram 消息" if owner else "Telegram 消息", "\n".join(lines), "telegram")
