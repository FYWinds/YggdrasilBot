"""Pages that are not the content: captcha, login and consent walls, and anti-bot block pages."""

import pytest

from yggdrasil.tools.web.blocks import detect_block, detect_wall


@pytest.mark.parametrize(
    ("title", "text"),
    [
        ("", "Please complete the CAPTCHA to continue"),
        # Google's cookie-consent page is long; the title alone identifies it.
        ("继续使用 YouTube 前须知", "我们使用 Cookie 和数据来提供和维护 Google 服务。" * 60),
    ],
)
def test_detects_walls(title: str, text: str) -> None:
    assert detect_wall(title, text) is not None


def test_long_article_mentioning_keywords_is_not_a_wall() -> None:
    text = "本文介绍短信验证码的实现原理。" + "正文内容。" * 500
    assert detect_wall("短信验证的实现", text) is None


def test_detects_block_pages() -> None:
    assert detect_block("Pardon Our Interruption", "") is not None
