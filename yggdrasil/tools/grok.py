"""X search through Grok: the xAI Responses API with its server-side ``x_search`` tool.

Grok runs the X keyword / semantic / user searches itself and answers with a summary plus ``url_citation``
annotations (the posts it used). Billing is per post / profile fetched plus tokens; the response reports the cost in
``usage.cost_in_usd_ticks`` (1e-10 USD). The xAI key is stored as the ``grok`` search provider; ``params.model``
picks the model. ``search_x`` falls back to the X API (``yggdrasil.tools.x``) when this is missing or fails.
"""

import time
from collections import OrderedDict
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import httpx

from yggdrasil.core.log import LOGGER
from yggdrasil.runtime import SearchProviderInfo
from yggdrasil.tools.errors import ToolError

KIND = "grok"
DEFAULT_BASE_URL = "https://api.x.ai"
DEFAULT_MODEL = "grok-4.7"
# grok-4.7 defaults to "high" (no "none"): 85–134 s per search in the 2026-11 trial, "low" 17–35 s at about half the
# cost with comparable answers. Provider param ``reasoning_effort`` overrides it.
DEFAULT_REASONING_EFFORT = "low"
MAX_HOURS = 720
_CN_TZ = timezone(timedelta(hours=8))
# Grok decides how many searches to run; at "high" effort a search can outlast this.
_TIMEOUT = httpx.Timeout(90, connect=10)
_MAX_OUTPUT_TOKENS = 1500
_CACHE_TTL = 600.0
_CACHE_SIZE = 32
_EXTRA_LINKS = 5
_INSTRUCTIONS = (
    "用 x_search 在 X 上查用户的问题，然后用简体中文回答。只写帖子里实际说了什么：谁说的（@用户名）、什么时候"
    "（换算成北京时间）、说了什么，每条后面附上帖子链接。帖子之间说法不一致就分别列出。转述别人的说法要写明是谁说的，"
    "不要当成事实。没找到完全对应的帖子就先直接说没找到，再列出找到的最接近的相关内容；不要编。"
    "不用 Markdown 标题和加粗。"
)


@dataclass(slots=True)
class Answer:
    text: str
    citations: list[str] = field(default_factory=list)
    """URLs of the posts Grok cited, in order of first citation."""
    cost_usd: float | None = None
    posts_fetched: int | None = None


_cache: OrderedDict[tuple[str, str, str, str], tuple[float, Answer]] = OrderedDict()


def provider(providers: Sequence[SearchProviderInfo]) -> SearchProviderInfo | None:
    """The highest-priority enabled ``grok`` provider with a key, or ``None``."""
    return next((p for p in providers if p.kind == KIND and p.enabled and p.api_key), None)


async def x_search(p: SearchProviderInfo, query: str, *, hours: int, client: httpx.AsyncClient | None = None) -> Answer:
    """Grok's answer to ``query`` from X posts of the last ``hours`` (day granularity). Raises :class:`ToolError`."""
    hours = max(1, min(hours, MAX_HOURS))
    since = (datetime.now(_CN_TZ) - timedelta(hours=hours)).date().isoformat()
    model = str(p.params.get("model") or DEFAULT_MODEL)
    effort = str(p.params.get("reasoning_effort") or DEFAULT_REASONING_EFFORT)
    key = (query, since, model, effort)
    if (hit := _cache.get(key)) is not None and time.monotonic() - hit[0] <= _CACHE_TTL:
        return hit[1]
    body = {
        "model": model,
        "reasoning": {"effort": effort},
        "input": [{"role": "system", "content": _INSTRUCTIONS}, {"role": "user", "content": query}],
        "tools": [{"type": "x_search", "from_date": since}],
        "max_output_tokens": _MAX_OUTPUT_TOKENS,
    }
    headers = {"Authorization": f"Bearer {p.api_key}"}
    url = p.base_url.rstrip("/") + "/v1/responses"
    try:
        if client is None:
            async with httpx.AsyncClient(timeout=_TIMEOUT) as own:
                resp = await own.post(url, json=body, headers=headers)
        else:
            resp = await client.post(url, json=body, headers=headers, timeout=_TIMEOUT)
    except httpx.TimeoutException as e:
        raise ToolError("Grok 搜索超时") from e
    except httpx.HTTPError as e:
        raise ToolError("Grok 无法访问") from e
    if resp.status_code != 200:
        raise ToolError(f"Grok 出错（HTTP {resp.status_code}{_error_detail(resp)}）")
    try:
        answer = parse_response(resp.json())
    except ValueError as e:
        raise ToolError("Grok 返回了无法识别的内容") from e
    LOGGER.info("grok x_search: {} posts, ${:.4f}", answer.posts_fetched, answer.cost_usd or 0)
    _cache[key] = (time.monotonic(), answer)
    while len(_cache) > _CACHE_SIZE:
        _cache.popitem(last=False)
    return answer


def parse_response(payload: object) -> Answer:
    """Text, cited URLs and cost of a Responses API payload; ``ValueError`` without an output message."""
    if not isinstance(payload, dict):
        raise ValueError("not an object")
    texts: list[str] = []
    citations: dict[str, None] = {}
    for item in payload.get("output") or []:
        if not isinstance(item, dict) or item.get("type") != "message":
            continue
        for part in item.get("content") or []:
            if not isinstance(part, dict) or part.get("type") != "output_text":
                continue
            texts.append(str(part.get("text") or ""))
            for ann in part.get("annotations") or []:
                if isinstance(ann, dict) and ann.get("type") == "url_citation" and isinstance(ann.get("url"), str):
                    citations[ann["url"]] = None
    text = "".join(texts).strip()
    if not text:
        raise ValueError("no output text")
    usage = u if isinstance(u := payload.get("usage"), dict) else {}
    details = d if isinstance(d := usage.get("server_side_tool_usage_details"), dict) else {}
    ticks = usage.get("cost_in_usd_ticks")
    posts = details.get("x_posts_fetched")
    return Answer(
        text=text,
        citations=list(citations),
        cost_usd=ticks / 1e10 if isinstance(ticks, int | float) else None,
        posts_fetched=posts if isinstance(posts, int) else None,
    )


def format_answer(answer: Answer) -> str:
    """The summary, then up to :data:`_EXTRA_LINKS` posts Grok found but did not link in the text."""
    lines = ["（以下是 Grok 在 X 上搜索后写的总结，可能有误；要核实细节就用 read_x 读帖子）", "", answer.text]
    extra = [url for url in answer.citations if url not in answer.text][:_EXTRA_LINKS]
    if extra:
        lines += ["", "其他搜到的帖子："] + [f"{i}. {url}" for i, url in enumerate(extra, 1)]
    return "\n".join(lines)


def _error_detail(resp: httpx.Response) -> str:
    try:
        body = resp.json()
    except ValueError:
        return ""
    detail = body.get("error") if isinstance(body, dict) else None
    if isinstance(detail, dict):
        detail = detail.get("message")
    return f"：{str(detail)[:200]}" if detail else ""
