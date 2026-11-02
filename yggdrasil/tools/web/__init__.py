"""``read_url``: fetch a web page (or a document link) and return readable, paginated text.

Pipeline: site adapter (official APIs, over httpx) -> Chrome-impersonating curl_cffi + trafilatura
(readability-lxml + markdownify fallback) -> the browser service (real Chrome: patchright, then nodriver) when the
page is a JS app, answers 401/403/429/503, or is an anti-bot block page / JS challenge. Captcha and login walls
raise :class:`ToolError` instead of returning junk; we never try to defeat captchas.
"""

from yggdrasil.tools.web.fetch import read_url
from yggdrasil.tools.web.model import Extracted, PageResult, RemoteFetch
from yggdrasil.tools.web.page import paginate

__all__ = ["Extracted", "PageResult", "RemoteFetch", "paginate", "read_url"]
