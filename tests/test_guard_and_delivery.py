import math
from types import SimpleNamespace

from yggdrasil.agent.schema import BotReply, Section, SourceRef
from yggdrasil.delivery.send import build_nodes, split_text, strip_markdown
from yggdrasil.moderation.guard import _score_from_logprobs, decide_action, parse_verdict
from yggdrasil.settings import InteractionSettings, ModerationCategoryRule, ModerationSettings


def tok(token: str, alts: dict[str, float]) -> SimpleNamespace:
    return SimpleNamespace(
        token=token, top_logprobs=[SimpleNamespace(token=t, logprob=math.log(p)) for t, p in alts.items()]
    )


def test_score_uses_token_after_safety_label_not_the_label_itself():
    logprobs = [
        tok("Safety", {"Safety": 1.0}),
        tok(":", {":": 1.0}),
        tok(" Safe", {" Safe": 0.6, " Unsafe": 0.3, " Cont": 0.1}),
    ]
    assert _score_from_logprobs(logprobs, "Safe") == round(0.3 + 0.05, 4)


def test_score_falls_back_without_logprobs():
    assert _score_from_logprobs(None, "Controversial") == 0.5
    assert _score_from_logprobs([], "Unsafe") == 1.0


def test_verdict_missing_when_guard_echoes_the_reply():
    # llama.cpp assistant prefill made the guard continue the reply instead of judging it.
    assert parse_verdict("Deno 团队整体加入 Cloudflare。结论：Deno 整个团队加入 Cloudflare，Deno 运行时") is None
    assert parse_verdict("Safety: Controversial\nCategories: Politically Sensitive Topics") == "Controversial"


def test_decide_action_takes_most_severe_rule_over_threshold():
    policy = ModerationSettings(
        default_rule=ModerationCategoryRule(threshold=0.9, action="review"),
        category_rules={
            "Jailbreak": ModerationCategoryRule(threshold=0.5, action="drop"),
            "PII": ModerationCategoryRule(threshold=0.2, action="review"),
        },
    )
    assert decide_action("Unsafe", 0.6, ["PII", "Jailbreak"], policy) == "drop"
    assert decide_action("Unsafe", 0.3, ["PII", "Jailbreak"], policy) == "review"
    assert decide_action("Unsafe", 0.6, ["Violent"], policy) == "pass"  # default threshold 0.9
    assert decide_action("Safe", 1.0, ["Jailbreak"], policy) == "pass"


def test_strip_markdown_keeps_text_and_link_targets():
    md = "## 标题\n**粗体** 和 *斜体*，见 [文档](https://x.dev/a)\n* 项目\n```py\nprint(1)\n```"
    out = strip_markdown(md)
    assert "#" not in out
    assert "**" not in out
    assert "```" not in out
    assert "文档 (https://x.dev/a)" in out
    assert "- 项目" in out
    assert "print(1)" in out


def test_split_text_respects_limit_and_keeps_content():
    text = "\n\n".join(["段落" * 30] * 5)
    parts = split_text(text, 130)
    assert all(len(p) <= 130 for p in parts)
    assert "".join(parts).replace("\n", "") == text.replace("\n", "")


def test_forward_nodes_order_sections_images_sources_metadata():
    reply = BotReply(
        mode="forward",
        title="t",
        sections=[Section(content="一段话")],
        sources=[SourceRef(label="src", url="https://e.x")],
    )
    nodes = build_nodes(reply, "meta", InteractionSettings(), self_id=1, nickname="b", images=[(b"\x89PNG", "图")])
    kinds = [n["data"]["content"][-1]["type"] for n in nodes]
    assert kinds == ["text", "image", "text", "text"]
    assert nodes[0]["data"]["content"][0]["data"]["text"] == "一段话"
    assert nodes[2]["data"]["content"][0]["data"]["text"].startswith("来源")
    assert nodes[-1]["data"]["content"][0]["data"]["text"] == "meta"


def test_forward_nodes_are_capped_by_merging_the_tail():
    reply = BotReply(mode="forward", sections=[Section(content=f"s{i}") for i in range(20)])
    cfg = InteractionSettings(forward_max_nodes=5, max_sections=10)
    nodes = build_nodes(reply, "meta", cfg, self_id=1, nickname="b")
    assert len(nodes) == 5
    assert "s19" in nodes[3]["data"]["content"][0]["data"]["text"]


def test_sections_over_the_cap_are_folded_into_the_last_one():
    reply = BotReply(mode="forward", sections=[Section(content=t * 2) for t in "abcd"])
    nodes = build_nodes(reply, None, InteractionSettings(max_sections=2), self_id=1, nickname="b")
    texts = [n["data"]["content"][0]["data"]["text"] for n in nodes]
    assert texts == ["aa", "bb\n\ncc\n\ndd"]
