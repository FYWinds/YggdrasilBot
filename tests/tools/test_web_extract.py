"""Title and text of an HTML page."""

import itertools
import time

from yggdrasil.tools.web.extract import extract_html

BODY = "<p>" + "这篇论文介绍了一个模型家族。" * 40 + "</p>"


def _page(head: str, body: str = BODY) -> str:
    return f"<html><head>{head}</head><body>{body}</body></html>"


def test_a_paper_with_hundreds_of_authors_is_read_at_once() -> None:
    # One citation_author tag per author, as on arXiv and journal pages. trafilatura's own metadata extraction
    # compares all of them with each other again for each one: 20 s for these 800, 12 minutes for arXiv's 3,300.
    letters = "bcdfghjklmnpqrstvwxz"
    triples = itertools.product(letters, "aeiou", letters)
    names = [f"{(a + b + c).title()}son, {(c + b + a).title()}a" for a, b, c in triples]
    authors = "".join(f'<meta name="citation_author" content="{name}"/>' for name in names[:800])
    page = _page(f'<title>Paper | Site</title><meta name="citation_title" content="A model family"/>{authors}')

    started = time.perf_counter()
    title, text = extract_html(page, "https://example.org/abs/1")

    assert time.perf_counter() - started < 5
    assert title == "A model family"
    assert "模型家族" in text


def test_the_title_is_the_shared_one_then_a_title_tag_then_the_heading() -> None:
    url = "https://example.org/post"
    og = '<meta property="og:title" content="分享标题"/>'
    named = '<meta name="twitter:title" content="推特标题"/>'
    assert extract_html(_page(named + og + "<title>页面标题 - 某站</title>"), url)[0] == "分享标题"
    assert extract_html(_page(named + "<title>页面标题 - 某站</title>"), url)[0] == "推特标题"
    assert extract_html(_page("<title>某站</title>", "<h1>文章标题</h1>" + BODY), url)[0] == "文章标题"
