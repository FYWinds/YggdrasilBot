import pytest

from yggdrasil.runtime import GroupInfo, Snapshot, TriggerRule
from yggdrasil.triggers.index import MatchInput, TriggerIndex

GROUP = 100


def rule(id: int, handler: str, match_type: str, patterns=(), **kw) -> TriggerRule:
    base = {
        "scope": "global",
        "group_id": None,
        "arg_template": None,
        "require_mention": False,
        "target": "quoted_or_self",
        "priority": 100,
        "cooldown_s": 0,
        "allowed_roles": (),
    }
    base.update(kw)
    return TriggerRule(id=id, handler=handler, match_type=match_type, patterns=tuple(patterns), **base)


def handler_of(resolved) -> str | None:
    return resolved.handler if resolved else None


def index(*rules: TriggerRule, group_settings: dict | None = None) -> TriggerIndex:
    groups = {GROUP: GroupInfo(GROUP, "g", True, group_settings or {})}
    return TriggerIndex(Snapshot(triggers=rules, groups=groups))


def inp(text: str, *, scope="group", to_me=False, replied=False, role="member") -> MatchInput:
    return MatchInput(
        scope=scope,
        group_id=GROUP if scope == "group" else None,
        text=text,
        to_me=to_me,
        replied_to_bot=replied,
        role=role,
    )


def test_prefix_strips_keyword_and_lower_priority_number_wins():
    idx = index(
        rule(1, "chat", "mention", priority=100),
        rule(2, "tldr", "keyword_prefix", ["tldr", "太长不看"], priority=10),
    )
    got = idx.match(inp("TLDR 这篇文章", to_me=True))
    assert got is not None
    assert (got.handler, got.args) == ("tldr", "这篇文章")


def test_group_rule_overrides_global_rules_of_same_handler_only():
    idx = index(
        rule(1, "tldr", "keyword_prefix", ["tldr"]),
        rule(2, "tldr", "keyword_prefix", ["总结"], scope="group", group_id=GROUP),
        rule(3, "chat", "keyword_prefix", ["ygg"]),
    )
    assert idx.match(inp("tldr x")) is None  # global tldr hidden in this group
    assert handler_of(idx.match(inp("总结 x"))) == "tldr"
    assert handler_of(idx.match(inp("ygg 你好"))) == "chat"  # other handlers still global


def test_require_mention_accepts_name_in_text():
    idx = index(rule(1, "privacy.optout", "keyword_exact", ["不要记我"], require_mention=True))
    assert idx.match(inp("不要记我")) is None
    assert handler_of(idx.match(inp("不要记我", to_me=True))) == "privacy.optout"
    named = index(rule(1, "chat", "name_mention"), group_settings={"persona": {"name": "小树", "aliases": []}}).match(
        inp("小树你好")
    )
    assert named is not None
    assert named.named


def test_regex_captures_feed_arg_template():
    idx = index(rule(1, "tldr", "regex", [r"总结最近(\d+)条"], arg_template="-n {0} {rest}"))
    got = idx.match(inp("帮我总结最近50条 重点看争论"))
    assert got is not None
    assert got.captures == ("50",)
    assert got.args == "-n 50 帮我 重点看争论"


@pytest.mark.parametrize(
    ("role", "allowed"),
    [("member", False), ("admin", True), ("owner", True), ("superuser", True)],
)
def test_allowed_roles_owner_implies_admin(role: str, allowed: bool):
    idx = index(rule(1, "chat", "keyword_exact", ["重载"], allowed_roles=("admin",)))
    assert (idx.match(inp("重载", role=role)) is not None) is allowed


def test_private_scope_only_sees_private_and_admin_privacy_globals():
    idx = index(
        rule(1, "chat", "keyword_prefix", ["ygg"]),
        rule(2, "admin.status", "keyword_exact", ["状态"]),
        rule(3, "admin.review", "keyword_prefix", ["通过"], scope="private", arg_template="approve {rest}"),
    )
    assert idx.match(inp("ygg hi", scope="private")) is None
    assert handler_of(idx.match(inp("状态", scope="private"))) == "admin.status"
    got = idx.match(inp("通过 R12", scope="private"))
    assert got is not None
    assert got.args == "approve R12"
