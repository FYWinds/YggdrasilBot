"""Pages that are not the content: captcha, login and cookie-consent walls, anti-bot block pages and JS
challenges."""

from yggdrasil.tools.web.model import Extracted, Unreadable

# Walls are short pages; keyword hits in long articles are ignored.
_WALL_MAX_CHARS = 2000
# Cookie-consent interstitials that replace the whole page: Google's EU wall (YouTube channel pages etc.) and IAB TCF
# dialogs such as MSN's. The browser shares our IP and gets them too.
_CONSENT_KEYWORDS = (
    "before you continue to",
    "继续使用 youtube 前须知",
    "number of partners (vendors)",
    "we and our partners process data",
)
_WALL_KEYWORDS = (
    "验证码",
    "安全验证",
    "人机验证",
    "滑动验证",
    "拖动滑块",
    "完成验证",
    "请登录",
    "登录后查看",
    "登录后继续",
    "环境异常",
    "访问异常",
    "请求存在异常",
    "限制本次访问",
    "异常流量",
    "访问过于频繁",
    "captcha",
    "verify you are human",
    "are you a robot",
    "you're not a robot",
    "prove your humanity",
    "unusual traffic",
    "just a moment...",
    "attention required",
    "checking your browser",
    "enable javascript and cookies",
    *_CONSENT_KEYWORDS,
)
# Anti-bot block and error pages that answer with a normal-looking page (often HTTP 200). Same short-page rule as
# the walls; unlike a wall, a real browser may still get through, so these escalate to the browser tier.
_BLOCK_KEYWORDS = (
    "blocked by network security",  # reddit
    "you've been blocked",
    "you have been blocked",
    "access denied",
    "request blocked",
    "pardon our interruption",  # Akamai
    "something went wrong",  # generic SPA error shells (e.g. behind Cloudflare)
    "请求已被拦截",  # CN WAFs (e.g. coolapk's HTTP 567 page)
    "安全策略拦截",
)
# Walls that are JavaScript challenges rather than captchas or logins: a better browser may pass them, so they
# escalate like block pages. Captcha / login walls never do (no captcha fighting; CN walls are IP reputation).
JS_CHALLENGES = frozenset({"just a moment...", "checking your browser", "enable javascript and cookies"})


def detect_wall(title: str, text: str) -> str | None:
    """Return the matched keyword if the page looks like a captcha / verification / login wall."""
    return _match_short_page(_WALL_KEYWORDS, title, text)


def detect_block(title: str, text: str) -> str | None:
    """Return the matched keyword if the page looks like an anti-bot block or error page."""
    return _match_short_page(_BLOCK_KEYWORDS, title, text)


def _match_short_page(keywords: tuple[str, ...], title: str, text: str) -> str | None:
    lowered_title = title.lower()
    for keyword in keywords:
        if keyword in lowered_title:
            return keyword
    if len(text) <= _WALL_MAX_CHARS:
        lowered = text.lower()
        for keyword in keywords:
            if keyword in lowered:
                return keyword
    return None


def raise_if_wall(extracted: Extracted) -> None:
    if keyword := detect_wall(extracted.title, extracted.text):
        if keyword in _CONSENT_KEYWORDS:
            raise Unreadable(f"页面被 Cookie 同意弹窗挡住了，读不到正文（检测到「{keyword}」）")
        raise Unreadable(f"该页面需要人机验证或登录（检测到「{keyword}」），无法读取")


def raise_if_blocked(extracted: Extracted) -> None:
    if keyword := detect_block(extracted.title, extracted.text):
        raise Unreadable(f"网站拒绝了自动访问（检测到「{keyword}」），无法读取")


def is_challenge(title: str, text: str) -> bool:
    """A block page or JS challenge: worth another (better) browser, unlike captcha and login walls."""
    return detect_wall(title, text) in JS_CHALLENGES or detect_block(title, text) is not None
