"""Labels for reaction emoji ids (QQ face ids < 10000, otherwise Unicode code points)."""

QQ_FACE_LABELS = {
    "4": "得意",
    "14": "微笑",
    "21": "可爱",
    "49": "拥抱",
    "63": "玫瑰",
    "66": "爱心",
    "74": "太阳",
    "76": "赞",
    "78": "握手",
    "89": "西瓜",
    "124": "OK",
    "144": "喝彩",
    "147": "棒棒糖",
    "171": "茶",
    "179": "doge",
    "201": "点赞",
    "282": "敬礼",
    "297": "拜谢",
    "318": "崇拜",
    "319": "比心",
    "424": "续标识",
}


def emoji_label(emoji_id: str) -> str:
    if emoji_id in QQ_FACE_LABELS:
        return QQ_FACE_LABELS[emoji_id]
    if emoji_id.isdigit() and int(emoji_id) > 1000:
        try:
            return chr(int(emoji_id))
        except (ValueError, OverflowError):
            pass
    return emoji_id
