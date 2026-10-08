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
