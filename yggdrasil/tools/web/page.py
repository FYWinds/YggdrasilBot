"""Pages of the text ``read_url`` returns, and where a URL's ``#fragment`` points in it."""

import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from yggdrasil.tools.web.model import Extracted

_PARA_SPLIT_RE = re.compile(r"\n\s*\n")


@dataclass(slots=True)
class Anchor:
    """Where a URL's ``#fragment`` points in the extracted text."""

    start: int
    label: str
    """The heading of the section it points at, if any."""


def paginate(text: str, max_chars: int) -> list[str]:
    """Split ``text`` into pages of at most ``max_chars``, on paragraph, then line, then hard boundaries."""
    blocks: list[str] = []
    for para in _PARA_SPLIT_RE.split(text.strip()):
        para = para.rstrip()  # not strip(): the first line keeps its indentation (code)
        if not para:
            continue
        if len(para) <= max_chars:
            blocks.append(para)
        else:
            blocks += _pack(_hard_lines(para, max_chars), "\n", max_chars)
    return _pack(blocks, "\n\n", max_chars) or [""]


def _hard_lines(para: str, limit: int) -> Iterable[str]:
    for line in para.split("\n"):
        for start in range(0, len(line), limit):
            yield line[start : start + limit]


def _pack(parts: Iterable[str], sep: str, limit: int) -> list[str]:
    out: list[str] = []
    current = ""
    for part in parts:
        candidate = f"{current}{sep}{part}" if current else part
        if len(candidate) <= limit:
            current = candidate
        else:
            if current:
                out.append(current)
            current = part
    if current:
        out.append(current)
    return out


# --------------------------------------------------------------------------- URL fragments

# Fragments that are client-side app state (``#/route``, ``#!/route``, ``#key=value&...``), not an anchor in the page.
_STATE_FRAGMENT_RE = re.compile(r"^[!/]|[=&]")
# Linear on crafted headings (200k spaces or "#"s): no lazy group before the $; trailing "#"s are stripped after.
_MD_HEADING_RE = re.compile(r"^#{1,6}[ \t]+(.+)$", re.MULTILINE)
_MD_LINK_RE = re.compile(r"\[([^\]]*)\]\([^)]*\)")
# Longer Markdown headings are not taken for a fragment's target; _MD_LINK_RE is quadratic on titles like "[[[[...".
_MD_HEADING_MAX = 300
_WORD_RE = re.compile(r"[^\W_]+")
_HEADING_TAGS = ("h1", "h2", "h3", "h4", "h5", "h6")
_TEXT_BLOCK_TAGS = frozenset({"p", "li", "dd", "dt", "td", "th", "pre", "blockquote", "figcaption"})
# A probe is the first words of a text block: this many word characters, and blocks with fewer than the minimum are
# skipped as not distinctive. At most _PROBE_BLOCKS blocks from the anchor on are tried.
_PROBE_CHARS = 60
_PROBE_MIN_CHARS = 16
_PROBE_BLOCKS = 20


def fragment_view(text: str, fragment: str, anchor: Anchor | None, max_chars: int) -> tuple[str, str]:
    """The text to paginate for a URL with ``#fragment``, and the note to show above page 1."""
    if not fragment:
        return text, ""
    if anchor is None:
        if _STATE_FRAGMENT_RE.search(fragment):
            return text, (
                f"（链接里的 #{fragment} 是网页脚本用的状态参数：它指向的视图要在浏览器里运行页面脚本才会显示，"
                "这里没有渲染出来，下面是页面的默认内容）"
            )
        return text, f"（链接里的 #{fragment} 在页面里没找到对应位置，下面从页面开头读起）"
    what = f"「{anchor.label}」" if anchor.label else "的位置"
    if anchor.start == 0 or len(text) <= max_chars:
        return text, f"（链接里的 #{fragment} 指向{what}，内容在本页中）"
    return text[anchor.start :], f"（链接里的 #{fragment} 指向{what}，下面从那里读起；前面的内容请读不带 # 的链接）"


def find_anchor(extracted: Extracted, fragment: str) -> Anchor | None:
    """Where ``#fragment`` points in the extracted text: the element with that id in the page source, else a
    Markdown heading whose GitHub-style slug it is (adapter text such as a README has no page source)."""
    if extracted.html and (anchor := _html_anchor(extracted.html, extracted.text, fragment)):
        return anchor
    wanted = fragment.lower()
    for match in _MD_HEADING_RE.finditer(extracted.text):
        heading = match.group(1).rstrip(" \t#")
        if len(heading) > _MD_HEADING_MAX:
            continue
        title = re.sub(r"[\\*`]", "", _MD_LINK_RE.sub(r"\1", heading)).strip()
        if re.sub(r"[^\w\- ]", "", title.lower()).replace(" ", "-") == wanted:
            return Anchor(match.start(), title)
    return None


def _html_anchor(html: str, text: str, fragment: str) -> Anchor | None:
    """The element with ``id`` (or ``<a name>``) ``fragment``, located in ``text`` by the first of its text blocks,
    or of the blocks after it, that survived extraction. Headings are not probed: extraction often drops them, and
    tables of contents repeat them."""
    from lxml import html as lxml_html
    from lxml.etree import ParserError

    try:
        root = lxml_html.fromstring(html)
    except (ParserError, ValueError):  # empty document, or an XML encoding declaration
        return None
    try:
        found = root.xpath("//*[@id=$f] | //a[@name=$f]", f=fragment)
    except ValueError:  # control characters (%00) or U+FFFE in the fragment: no element can have that id
        return None
    if not found:
        return None
    target = found[0]
    heading = next(target.iter(*_HEADING_TAGS), None)
    label = " ".join(heading.text_content().split())[:80] if heading is not None else ""
    start = _first_block_match(root, target, text)
    if start is None:
        return None
    start = text.rfind("\n", 0, start) + 1
    # Start at the section heading when the extraction kept it just above.
    if label and (probe := _probe(label, 1)) and (hits := list(probe.finditer(text, max(0, start - 300), start))):
        start = text.rfind("\n", 0, hits[-1].start()) + 1
    return Anchor(start, label)


def _first_block_match(root: Any, target: Any, text: str) -> int | None:
    started, tried = False, 0
    for el in root.iter():
        started = started or el is target
        if not started or el.tag not in _TEXT_BLOCK_TAGS:
            continue
        if (probe := _probe(el.text_content(), _PROBE_MIN_CHARS)) is None:
            continue
        if (match := probe.search(text)) is not None:
            return match.start()
        tried += 1
        if tried == _PROBE_BLOCKS:
            break
    return None


def _probe(content: str, min_chars: int) -> re.Pattern[str] | None:
    """A pattern for the first words of ``content`` that allows the punctuation, Markdown escapes and line breaks
    extraction puts between them; ``None`` for fewer than ``min_chars`` word characters."""
    words: list[str] = []
    size = 0
    for word in _WORD_RE.findall(content):
        words.append(re.escape(word[: _PROBE_CHARS - size]))
        size += len(word)
        if size >= _PROBE_CHARS:
            break
    return re.compile(r"[\W_]+".join(words)) if size >= min_chars else None
