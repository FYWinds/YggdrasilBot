"""Hugging Face: model and dataset facts from the API, then the card."""

import asyncio
import re

import httpx

from yggdrasil.tools.web.adapters.common import cn_count, fragment_text, optional_get, optional_json
from yggdrasil.tools.web.model import AdapterResult, Extracted

_HUGGING_FACE = "https://huggingface.co"
# First path segments of huggingface.co pages that are not a user or an organization.
_HF_RESERVED = frozenset(
    "api blog chat collections docs enterprise join learn login models new organizations papers posts pricing "
    "settings spaces tasks".split()
)
_FRONT_MATTER_RE = re.compile(r"\A---[ \t]*\r?\n.*?\r?\n---[ \t]*(?:\r?\n|\Z)", re.DOTALL)
_HF_MAX_FILES = 40


async def repo(client: httpx.AsyncClient, url: httpx.URL) -> AdapterResult:
    """A model or dataset: facts from the API, then its card (the README). The page opens with the "use this
    model" widgets or the dataset viewer, which extraction takes for the content."""
    segments = [s for s in url.path.split("/") if s]
    kind = "models"
    if segments[:1] == ["datasets"]:
        kind, segments = "datasets", segments[1:]
    if len(segments) != 2 or segments[0] in _HF_RESERVED:
        return None
    repo = "/".join(segments)
    page = f"{_HUGGING_FACE}/{repo}" if kind == "models" else f"{_HUGGING_FACE}/datasets/{repo}"
    info, readme = await asyncio.gather(
        optional_json(client.get(f"{_HUGGING_FACE}/api/{kind}/{repo}"), "hugging face api"),
        optional_get(client.get(f"{page}/raw/main/README.md"), "hugging face readme"),
    )
    if not isinstance(info, dict):
        return None  # private, missing, or the API is down: the generic path decides
    card = _FRONT_MATTER_RE.sub("", readme.text).strip() if readme is not None else ""
    if not card:  # gated repositories show their card only on the page
        html = await optional_get(client.get(page), "hugging face page")
        card = _hf_page_card(html.text) if html is not None else ""
    title = f"{info.get('id') or repo} · Hugging Face"
    return Extracted(page, title, format_hugging_face(kind, info, card), "huggingface")


def format_hugging_face(kind: str, info: dict, card: str) -> str:
    noun = "数据集" if kind == "datasets" else "模型"
    card_data = raw if isinstance(raw := info.get("cardData"), dict) else {}
    lines = [f"{noun}：{info.get('id')}"]
    facts = [f"任务 {info['pipeline_tag']}"] if info.get("pipeline_tag") else []
    if library := info.get("library_name"):
        facts.append(f"库 {library}")
    if isinstance(safetensors := info.get("safetensors"), dict) and safetensors.get("total"):
        facts.append(f"参数量 {_hf_params(int(safetensors['total']))}")
    gguf = raw if isinstance(raw := info.get("gguf"), dict) else {}
    if gguf.get("architecture"):
        facts.append(f"GGUF 架构 {gguf['architecture']}")
    if gguf.get("context_length"):
        facts.append(f"上下文长度 {gguf['context_length']}")
    if facts:
        lines.append(" · ".join(facts))
    if license_ := card_data.get("license"):
        name = card_data.get("license_name")
        lines.append(f"许可证：{license_}（{name}）" if name else f"许可证：{license_}")
    if base := card_data.get("base_model"):
        bases = [base] if isinstance(base, str) else [str(b) for b in base]
        relation = card_data.get("base_model_relation")
        lines.append("基础模型：" + "、".join(bases) + (f"（{relation}）" if relation else ""))
    stats = [f"近 30 天下载 {cn_count(int(info['downloads']))}"] if info.get("downloads") is not None else []
    if info.get("likes") is not None:
        stats.append(f"点赞 {info['likes']}")
    if modified := info.get("lastModified"):
        stats.append(f"更新于 {str(modified)[:10]}")
    if stats:
        lines.append(" · ".join(stats))
    if info.get("gated"):
        lines.append("（受限仓库：要登录并同意作者的条款才能下载文件）")
    files = [str(s["rfilename"]) for s in info.get("siblings") or [] if isinstance(s, dict) and s.get("rfilename")]
    if files and kind == "datasets":
        lines.append(f"文件：{len(files)} 个")  # data shards: the names say nothing
    elif files:
        more = "……" if len(files) > _HF_MAX_FILES else ""
        lines.append(f"文件（{len(files)} 个）：" + "、".join(files[:_HF_MAX_FILES]) + more)
    lines += ["", f"{noun}卡（README）：", "", card] if card else ["", f"（没有{noun}卡）"]
    return "\n".join(lines)


def _hf_params(total: int) -> str:
    for unit, size in (("T", 10**12), ("B", 10**9), ("M", 10**6)):
        if total >= size:
            return f"{total / size:.1f}{unit}"
    return str(total)


def _hf_page_card(html: str) -> str:
    """The model or dataset card as rendered on the repository page, in Markdown."""
    from lxml import html as lxml_html
    from lxml.etree import ParserError

    try:
        root = lxml_html.fromstring(html)
    except (ParserError, ValueError):
        return ""
    cards = root.xpath(
        "//div[contains(concat(' ', normalize-space(@class), ' '), ' model-card-content ')"
        " or contains(concat(' ', normalize-space(@class), ' '), ' hf-sanitized ')]"
    )
    if not cards:
        return ""
    card = max(cards, key=lambda el: len(el.text_content()))
    return fragment_text(str(lxml_html.tostring(card, encoding="unicode")))
