import json

from yggdrasil.ingest.normalize import normalize


def test_self_sent_forward_card_becomes_forward_attachment():
    card = {"app": "com.tencent.multimsg", "meta": {"detail": {"resid": "abc123", "news": []}}}
    norm = normalize([{"type": "json", "data": {"data": json.dumps(card)}}])
    assert norm.text == "[合并转发]"
    assert [(a.kind, a.data) for a in norm.attachments] == [("forward", {"id": "abc123"})]


def test_other_json_cards_stay_cards():
    card = {"app": "com.tencent.miniapp", "prompt": "分享", "meta": {"x": {"title": "标题", "jumpUrl": "https://a.b"}}}
    norm = normalize([{"type": "json", "data": {"data": json.dumps(card)}}])
    assert norm.text == "[卡片:标题]"
    assert norm.attachments[0].kind == "card"


def test_markdown_from_another_bot_shows_its_text_once():
    body = "查了，没有权限。\n不是我不愿意。"
    norm = normalize([{"type": "markdown", "data": {"content": body}}, {"type": "text", "data": {"text": body}}])
    assert norm.text == f"[markdown]{body}"


def test_market_face_sticker_shows_one_label():
    face = {
        "key": "0123456789abcdef",
        "url": "https://example.com/item/raw300.gif",
        "file": "a6-0123.gif",
        "summary": "[加载中]",
        "emoji_id": "0123456789abcdef0123456789abcdef",
        "sub_type": 0,
        "emoji_package_id": 200001,
    }
    norm = normalize([{"type": "image", "data": face}, {"type": "text", "data": {"text": "[加载中]"}}])
    assert norm.text == "[表情包:加载中]"
    assert [a.kind for a in norm.attachments] == ["image"]


def test_text_after_a_sticker_is_kept():
    face = {"summary": "[加载中]", "emoji_id": "0123", "sub_type": 0}
    norm = normalize([{"type": "image", "data": face}, {"type": "text", "data": {"text": "等一下"}}])
    assert norm.text == "[表情包:加载中]等一下"
