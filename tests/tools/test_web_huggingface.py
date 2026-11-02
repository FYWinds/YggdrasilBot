"""The Hugging Face adapter of ``read_url``: model and dataset facts from the API, then the card."""

import httpx

from tests.support.routes import Routes
from yggdrasil.tools.web import Extracted
from yggdrasil.tools.web.adapters import huggingface

HF_MODEL = {
    "id": "acme/tiny-gguf",
    "pipeline_tag": "text-generation",
    "library_name": "gguf",
    "downloads": 123456,
    "likes": 42,
    "lastModified": "2026-01-05T00:00:00.000Z",
    "cardData": {"license": "apache-2.0", "base_model": "acme/tiny", "base_model_relation": "quantized"},
    "gguf": {"architecture": "llama", "context_length": 32768},
    "siblings": [{"rfilename": "README.md"}, {"rfilename": "tiny-Q4_K_M.gguf"}],
    "widgetData": [{"text": "Hi, what can you help me with?"}],
}
HF_README = "---\nlicense: apache-2.0\nbase_model: acme/tiny\n---\n\n# Tiny GGUF\n\nRuns at 200 tok/s on one GPU.\n"


async def test_hugging_face_model_gives_the_card_not_the_widgets() -> None:
    routes = Routes(
        {
            "https://huggingface.co/api/models/acme/tiny-gguf": httpx.Response(200, json=HF_MODEL),
            "https://huggingface.co/acme/tiny-gguf/raw/main/README.md": httpx.Response(200, text=HF_README),
        }
    )
    async with routes.client() as client:
        result = await huggingface.repo(client, httpx.URL("https://huggingface.co/acme/tiny-gguf"))
        for other in ("https://huggingface.co/spaces/acme/demo", "https://huggingface.co/acme"):
            assert await huggingface.repo(client, httpx.URL(other)) is None
    assert isinstance(result, Extracted)
    assert (result.title, result.via) == ("acme/tiny-gguf · Hugging Face", "huggingface")
    assert result.text == (
        "模型：acme/tiny-gguf\n"
        "任务 text-generation · 库 gguf · GGUF 架构 llama · 上下文长度 32768\n"
        "许可证：apache-2.0\n"
        "基础模型：acme/tiny（quantized）\n"
        "近 30 天下载 12.3万 · 点赞 42 · 更新于 2026-01-05\n"
        "文件（2 个）：README.md、tiny-Q4_K_M.gguf\n"
        "\n"
        "模型卡（README）：\n"
        "\n"
        "# Tiny GGUF\n\nRuns at 200 tok/s on one GPU."
    )
    assert len(routes.seen) == 2  # the API and the README; the page is not needed


async def test_hugging_face_gated_card_comes_from_the_page() -> None:
    page = (
        "<html><body><div class='widgets'>Use this model · Instructions to use acme/corpus with libraries</div>"
        "<div class='model-card-content prose'><h2>Dataset Summary</h2><p>Ten million example sentences.</p></div>"
        "</body></html>"
    )
    routes = Routes(
        {
            "https://huggingface.co/api/datasets/acme/corpus": httpx.Response(
                200, json={"id": "acme/corpus", "gated": "manual", "likes": 3}
            ),
            "https://huggingface.co/datasets/acme/corpus/raw/main/README.md": httpx.Response(
                401, text="Access to dataset acme/corpus is restricted."
            ),
            "https://huggingface.co/datasets/acme/corpus": httpx.Response(200, html=page),
        }
    )
    async with routes.client() as client:
        result = await huggingface.repo(client, httpx.URL("https://huggingface.co/datasets/acme/corpus"))
    assert isinstance(result, Extracted)
    assert result.final_url == "https://huggingface.co/datasets/acme/corpus"
    assert "（受限仓库：要登录并同意作者的条款才能下载文件）" in result.text
    assert result.text.endswith("数据集卡（README）：\n\n## Dataset Summary\n\nTen million example sentences.")
    assert "Use this model" not in result.text
