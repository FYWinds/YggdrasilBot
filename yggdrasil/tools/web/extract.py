"""Readable text from an HTML page or a JSON body."""

import json
import re
from typing import Any
from urllib.parse import urlsplit

from yggdrasil.tools.docs import clip_text, decode_text

# Extracted text shorter than this is treated as a JS shell worth rendering in the browser.
MIN_TEXT_CHARS = 200
# Lines that are only stray punctuation (left over from stripped inline widgets).
_JUNK_LINE_RE = re.compile(r"^[ \t]*[，。、：；！？,.:;!?·…]{1,3}[ \t]*(?:\n|$)", re.MULTILINE)


def extract_html(html: str, url: str) -> tuple[str, str]:
    """Return ``(title, markdown-ish text)`` of the main content of an HTML document."""
    import trafilatura

    text = (
        trafilatura.extract(
            html,
            url=url,
            output_format="markdown",
            include_tables=True,
            include_links=False,
            include_images=False,
            include_comments=False,
            favor_recall=True,
            deduplicate=True,
        )
        or ""
    ).strip()
    meta = trafilatura.extract_metadata(html, default_url=url)
    title = (meta.title or "") if meta is not None else ""
    if len(text) < MIN_TEXT_CHARS:
        fallback_title, fallback = _readability(html)
        if len(fallback) > len(text):
            text = fallback
        title = title or fallback_title
    return title.strip(), _JUNK_LINE_RE.sub("", text).strip()


def _readability(html: str) -> tuple[str, str]:
    from markdownify import markdownify
    from readability import Document

    try:
        doc = Document(html)
        summary = doc.summary(html_partial=True)
        title = doc.short_title()
    except Exception:
        return "", ""
    text = markdownify(summary, heading_style="ATX", strip=["a", "img"])
    return title or "", re.sub(r"\n{3,}", "\n\n", text).strip()


# JSON fields that only matter to programs: global node ids, gravatar hashes, HAL link sections.
_JSON_NOISE_KEYS = frozenset({"node_id", "gravatar_id", "_links"})
# Objects and lists of plain values up to this long are written on one line: {a: 1, b: x} / [x, y].
_JSON_INLINE_MAX = 100
# The text budget after compaction, as for other documents (parse_document's default).
_JSON_MAX_CHARS = 60000


def json_text(data: bytes, charset: str | None) -> tuple[str, bool] | None:
    """A JSON body as YAML-like text without noise fields or empty values, and whether it was clipped; ``None``
    if it is not JSON. API responses (api.github.com) are mostly cross-links, ids and signatures otherwise."""
    decoded = decode_text(data, charset)
    if decoded is None:
        return None
    try:
        value = json.loads(decoded)
        pruned = _prune_json(value)
        trimmed = pruned != value
        if pruned in (None, "", [], {}):
            body = json.dumps(value, ensure_ascii=False)[:500]  # nothing left: show what there was
        else:
            body = "\n".join(_json_lines(pruned, ""))
    except (ValueError, RecursionError):  # not JSON, or nested too deep to walk: read as plain text
        return None
    note = "（JSON 数据）"
    if trimmed:
        note = "（JSON 数据，已精简：去掉了空值、node_id、头像、API 链接和签名等字段）"
    text, clipped = clip_text(body, _JSON_MAX_CHARS)
    return f"{note}\n\n{text}", clipped


def _prune_json(value: Any, parent: str = "") -> Any:
    """``value`` without noise fields, and without object fields whose value is empty (null, "", [], {}). List
    elements all stay: their positions can matter (a series lined up with timestamps, table rows)."""
    if isinstance(value, dict):
        kept: dict[str, Any] = {}
        for key, item in value.items():
            if _json_noise(parent, str(key), item):
                continue
            item = _prune_json(item, str(key))
            if item not in (None, "", [], {}):
                kept[str(key)] = item
        return kept
    if isinstance(value, list):
        return [_prune_json(item, parent) for item in value]
    return value


def _json_noise(parent: str, key: str, value: Any) -> bool:
    if key in _JSON_NOISE_KEYS:
        return True
    if not isinstance(value, str):
        return False
    if value.startswith("-----BEGIN "):
        return True  # PGP / SSH signatures, certificates
    if parent == "verification" and key == "payload":
        return True  # the signed raw git object of a GitHub commit
    if key == "url" or key.endswith("_url"):
        # API cross-links and URI templates (".../following{/other_user}") are for programs; avatars are images.
        return "{" in value or "avatar" in key or _is_api_link(value)
    return False


def _is_api_link(value: str) -> bool:
    try:
        parts = urlsplit(value)
    except ValueError:
        return False
    return (parts.hostname or "").startswith("api.") or parts.path.startswith("/api/")


def _json_lines(value: Any, pad: str) -> list[str]:
    """``value`` as YAML-like lines indented by ``pad``. Multi-line items of the outer lists (a list response, or the
    list in a search response) are separated by blank lines, so pages break between items."""
    if isinstance(value, dict):
        lines: list[str] = []
        for key, item in value.items():
            if (inline := _json_inline(item)) is not None:
                lines.append(f"{pad}{key}: {inline}")
            else:
                lines += [f"{pad}{key}:", *_json_lines(item, pad + "  ")]
        return lines
    if isinstance(value, list):
        lines = []
        for item in value:
            if (inline := _json_inline(item)) is not None:
                lines.append(f"{pad}- {inline}")
                continue
            if lines and len(pad) <= 2:
                lines.append("")
            first, *rest = _json_lines(item, pad + "  ")
            lines += [f"{pad}- {first.lstrip()}", *rest]
        return lines
    return [pad + _json_scalar(value)]


def _json_inline(value: Any) -> str | None:
    """A plain value, or a short object / list of plain values, on one line; ``None`` if it needs several."""
    if not isinstance(value, dict | list):
        return _json_scalar(value)
    items = list(value.values()) if isinstance(value, dict) else value
    if any(isinstance(item, dict | list) for item in items):
        return None
    if isinstance(value, dict):
        inline = "{" + ", ".join(f"{k}: {_json_scalar(v)}" for k, v in value.items()) + "}"
    else:
        inline = "[" + ", ".join(_json_scalar(v) for v in value) + "]"
    return inline if len(inline) <= _JSON_INLINE_MAX else None


def _json_scalar(value: Any) -> str:
    if isinstance(value, str):
        # Bare unless line breaks or edge spaces would make it ambiguous.
        if value != value.strip() or re.search(r"[\n\r\t]", value):
            return json.dumps(value, ensure_ascii=False)
        return value
    return json.dumps(value)
