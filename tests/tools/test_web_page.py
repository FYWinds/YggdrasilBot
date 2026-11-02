"""Pages of the text ``read_url`` returns, and URL ``#fragments`` that start the pages at a section."""

import time
from collections.abc import Callable

import httpx
import pytest

from tests.support.routes import Routes
from yggdrasil.tools.web import Extracted, paginate, read_url

type LocalTier = Callable[[Extracted | Exception], list[str]]

# --------------------------------------------------------------------------- pagination


def test_short_text_is_one_page() -> None:
    assert paginate("  hello\n\nworld  ", 100) == ["hello\n\nworld"]


def test_empty_text_is_one_empty_page() -> None:
    assert paginate("\n\n  \n", 100) == [""]


def test_paragraphs_pack_up_to_exact_limit() -> None:
    text = "a" * 50 + "\n\n" + "b" * 50
    assert paginate(text, 102) == [text]
    assert paginate(text, 101) == ["a" * 50, "b" * 50]


def test_oversized_paragraph_splits_on_lines_then_hard() -> None:
    para = "x" * 30 + "\n" + "y" * 250
    pages = paginate(para, 100)
    assert pages == ["x" * 30, "y" * 100, "y" * 100, "y" * 50]


def test_pagination_keeps_indentation_of_code_after_blank_lines() -> None:
    code = "class A:\n    def f(self):\n        pass\n\n    def g(self):\n        pass"
    assert paginate(code, 1000) == [code]
    assert paginate(code, 30) == ["class A:\n    def f(self):", "        pass", "    def g(self):\n        pass"]


# --------------------------------------------------------------------------- URL fragments

SPEC = """<html><head><title>Example Spec</title></head><body><main>
<nav><ul><li><a href="#section-1">1. Background</a></li><li><a href="#section-2">2. Timestamps</a></li></ul></nav>
<section id="section-1"><h2 id="name-background"><a href="#section-1">1. </a><a href="#name-background">Background</a>
</h2>{first}</section>
<section id="section-2"><h2 id="name-timestamps"><a href="#section-2">2. </a><a href="#name-timestamps">Timestamps</a>
</h2>{second}</section>
</main></body></html>""".format(
    first="".join(f"<p>第一节正文第 {i} 段，介绍背景和术语，内容足够长，抽取器会保留它。</p>" for i in range(8)),
    second="".join(f"<p>第二节正文第 {i} 段，说明时间戳的编码方式，内容同样足够长。</p>" for i in range(8)),
)


@pytest.fixture
def spec(web_page: Routes) -> None:
    web_page.answers["example.com"] = httpx.Response(200, html=SPEC)


@pytest.mark.usefixtures("spec")
async def test_fragment_starts_the_pages_at_its_section() -> None:
    result = await read_url("https://example.com/spec.html#section-2", max_chars=300)
    note, body = result.text.split("\n\n", 1)
    assert note == "（链接里的 #section-2 指向「2. Timestamps」，下面从那里读起；前面的内容请读不带 # 的链接）"
    assert "第二节正文第 0 段" in body[:60]
    assert "第一节" not in body
    plain = await read_url("https://example.com/spec.html", max_chars=300)
    assert plain.text.startswith(("第一节正文第 0 段", "## 1. Background"))


@pytest.mark.usefixtures("spec")
@pytest.mark.parametrize(
    ("fragment", "says"),
    [
        ("category=code_v3&dataset=code_v3|2026-10|0", "是网页脚本用的状态参数"),
        ("no-such-anchor", "在页面里没找到对应位置"),
    ],
)
async def test_unresolved_fragment_is_explained(fragment: str, says: str) -> None:
    result = await read_url(f"https://example.com/spec.html#{fragment}", max_chars=300)
    note, body = result.text.split("\n\n", 1)
    assert note.startswith(f"（链接里的 #{fragment} {says}")
    assert "第一节正文第 0 段" in body


@pytest.mark.usefixtures("spec")
async def test_fragment_that_no_element_can_have_still_reads_the_page() -> None:
    # A control character is not an XML string: no element can have that id.
    note, body = (await read_url("https://example.com/spec.html#a%00b", max_chars=300)).text.split("\n\n", 1)
    assert note.endswith("在页面里没找到对应位置，下面从页面开头读起）")
    assert "第一节正文第 0 段" in body


@pytest.mark.usefixtures("spec")
async def test_text_fragment_is_not_called_script_state() -> None:
    plain = await read_url("https://example.com/spec.html", max_chars=300)
    highlight = await read_url("https://example.com/spec.html#:~:text=第二节正文第%203%20段,编码方式", max_chars=300)
    assert highlight.text == plain.text
    both = await read_url("https://example.com/spec.html#section-2:~:text=nothing", max_chars=300)
    assert both.text.startswith("（链接里的 #section-2 指向「2. Timestamps」")


def _readme(text: str) -> Extracted:
    """What an adapter returns for a README: Markdown text without page source."""
    return Extracted("https://example.com/acme/widget", "acme/widget", text, "github")


async def test_fragment_matches_a_markdown_heading_without_page_source(local_tier: LocalTier) -> None:
    intro = "\n\n".join(["# Widget", *[f"Intro paragraph {i} about the widget." for i in range(10)]])
    local_tier(_readme(intro + "\n\n## Getting Started\n\nRun `widget init` first.\n\n## License\n\nMIT"))
    result = await read_url("https://example.com/acme/widget#getting-started", max_chars=200)
    note, body = result.text.split("\n\n", 1)
    assert note.startswith("（链接里的 #getting-started 指向「Getting Started」")
    assert body.startswith("## Getting Started\n\nRun `widget init` first.")


async def test_crafted_markdown_headings_do_not_stall_the_lookup(local_tier: LocalTier) -> None:
    crafted = ["# a" + " " * 200_000 + "b", "## a" + "#" * 200_000 + "b", "# " + "[" * 200_000]
    local_tier(_readme("\n\n".join([*crafted, "## Usage", "Run it."])))
    started = time.perf_counter()
    result = await read_url("https://example.com/acme/widget#usage", max_chars=1000)
    assert time.perf_counter() - started < 5  # quadratic heading scans take minutes
    assert result.text == (
        "（链接里的 #usage 指向「Usage」，下面从那里读起；前面的内容请读不带 # 的链接）\n\n## Usage\n\nRun it."
    )
