"""Pages that are not the content: captcha, login and consent walls, and anti-bot block pages."""

import pytest

from yggdrasil.tools.web.blocks import detect_block, detect_wall


@pytest.mark.parametrize(
    ("title", "text"),
    [
        ("", "环境异常\n当前环境异常，完成验证后即可继续访问。\n去验证"),
        ("安全验证 - 知乎", ""),
        ("Just a moment...", "Checking if the site connection is secure"),
        ("", "Please complete the CAPTCHA to continue"),
        ("登录", "请登录后查看完整内容"),
        # Google's cookie-consent page is long; the title alone identifies it.
        ("继续使用 YouTube 前须知", "我们使用 Cookie 和数据来提供和维护 Google 服务。" * 60),
        ("Before you continue to YouTube", ""),
    ],
)
def test_detects_walls(title: str, text: str) -> None:
    assert detect_wall(title, text) is not None


def test_long_article_mentioning_keywords_is_not_a_wall() -> None:
    text = "本文介绍短信验证码的实现原理。" + "正文内容。" * 500
    assert detect_wall("短信验证的实现", text) is None


@pytest.mark.parametrize(
    ("title", "text"),
    [
        ("Reddit - Dive into anything", "You've been blocked by network security.\nTo continue, log in."),
        ("Pardon Our Interruption", ""),
        ("", "Something went wrong. Try reloading."),
    ],
)
def test_detects_block_pages(title: str, text: str) -> None:
    assert detect_block(title, text) is not None


def test_long_article_mentioning_access_denied_is_not_a_block() -> None:
    text = "How we debugged an access denied error in S3. " + "Body text. " * 300
    assert detect_block("S3 permissions", text) is None
