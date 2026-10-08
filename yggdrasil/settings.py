"""Customizable behaviour, stored as JSON in the database.

The global document (``settings`` row ``global``) is validated by :class:`Settings`;
a group's ``groups.settings`` is deep-merged over it. The dashboard renders forms
from ``Settings.model_json_schema()``, so every field here is editable.
"""

from typing import Any, Literal

from pydantic import BaseModel, Field


class PersonaExample(BaseModel):
    user: str
    bot: str


class PersonaSettings(BaseModel):
    name: str = Field(default="尤克特拉希尔", description="bot 的名字，出现在提示词里")
    aliases: list[str] = Field(
        default_factory=lambda: ["ygg", "世界树"], description="点名触发词（name_mention 规则使用）"
    )
    description: str = Field(
        default="一个懂行、靠谱、有点毒舌但心软的老群友。说话轻松口语化，偶尔吐槽，但从不敷衍正经问题。",
        description="人设：性格、说话风格、口癖等，会写进所有对话类提示词",
    )
    examples: list[PersonaExample] = Field(
        default_factory=lambda: [
            PersonaExample(user="这个报错啥意思", bot="依赖版本对不上了，你把 lock 文件删了重装一下试试。"),
            PersonaExample(user="中午吃什么", bot="这种世纪难题别问我，抛硬币吧。"),
        ],
        description="对话示例，用来固定语气（每条 bot 回复应符合风格规则）",
    )


class InteractionSettings(BaseModel):
    ack_delay_s: float = Field(default=4.5, ge=0, description="agent 超过这个时间没完成才发快速响应")
    react_emoji_whitelist: list[str] = Field(
        default_factory=lambda: ["76", "124", "128064", "128077", "129300", "128514", "10068", "128170"],
        description="tldr 快速响应可用的表情 id（QQ face id 或 Unicode 码点）",
    )
    react_default: str = Field(default="128064", description="quick 模型失败时用的表情（👀）")
    input_blocked_emoji: str = Field(default="10060", description="输入被审核拦截时 react 的表情（❌）")
    sticker_ack: bool = Field(default=True, description="快速响应允许发表情包")
    messages_max_count: int = Field(default=3, ge=1, le=10, description="短消息模式最多连发几条，超出改用合并转发")
    message_max_chars: int = Field(default=20, ge=1, description="短消息模式下每条的字数上限，任一条超出就改用合并转发")
    message_interval_s: float = Field(default=0.3, ge=0, description="连发短消息之间的间隔")
    forward_max_nodes: int = Field(default=12, ge=1, le=60)
    node_max_chars: int = Field(default=1500, ge=200)
    forward_render: Literal["plain", "nested_markdown"] = Field(
        default="plain", description="plain：去掉 markdown 语法；nested_markdown：双层合并转发里发 markdown 段"
    )
    failure_emoji: str = Field(default="128531", description="agent 出错时 react 的表情（😓）")
    ratelimited_emoji: str = Field(default="9203", description="被限流时 react 的表情（⏳）；留空不 react")
    quote_reply: Literal["always", "when_busy", "never"] = Field(
        default="when_busy",
        description="回复时是否引用触发消息：always 总是；when_busy 触发消息之后群里已有别人发言才引用；never 从不",
    )
    metadata_node: bool = True
    metadata_fields: list[Literal["model", "tokens", "tools", "latency", "moderation", "trace"]] = Field(
        default_factory=lambda: ["model", "tokens", "tools", "latency", "moderation", "trace"]
    )


class ModerationCategoryRule(BaseModel):
    threshold: float = Field(default=0.5, ge=0, le=1)
    action: Literal["pass", "review", "drop"] = "review"


class ModerationSettings(BaseModel):
    enabled: bool = True
    check_input: bool = True
    check_ack: bool = True
    check_output: bool = True
    default_rule: ModerationCategoryRule = Field(default_factory=ModerationCategoryRule)
    category_rules: dict[str, ModerationCategoryRule] = Field(
        default_factory=lambda: {
            "Politically Sensitive Topics": ModerationCategoryRule(threshold=0.4, action="review"),
            "Sexual Content or Sexual Acts": ModerationCategoryRule(threshold=0.4, action="review"),
            "Jailbreak": ModerationCategoryRule(threshold=0.6, action="drop"),
        },
        description="按 Qwen3Guard 类别覆盖阈值和动作",
    )
    input_action_on_hit: Literal["drop", "pass"] = Field(default="drop", description="输入超阈值时：不响应 / 放行")
    notify_admins_on_input_block: bool = False
    notify_group_on_review: bool = Field(default=True, description="进入审核时群里先回一句（模型生成）")
    review_expire_minutes: int = Field(default=120, ge=5)


class RateLimitSettings(BaseModel):
    user_per_minute: int = 4
    user_per_day: int = 80
    group_per_minute: int = 20
    group_daily_tokens: int = 3_000_000
    max_concurrent_per_group: int = Field(default=4, ge=1, description="每个群同时运行的请求数，超出排队")
    max_concurrent_per_user: int = Field(default=2, ge=1, description="每个人同时运行的请求数，超出排队")
    max_queued_per_user: int = Field(default=3, ge=0, description="每个人最多排队几个请求，再多就丢弃")
    queue_timeout_s: float = Field(default=180, gt=0, description="排队超过这个时间就放弃")


class ContextSettings(BaseModel):
    recent_messages: int = Field(default=40, ge=0, le=300)
    recent_minutes: int = Field(default=30, ge=0)
    max_context_chars: int = Field(default=12000, ge=1000)
    include_profiles: bool = True
    include_glossary: bool = True
    include_open_topics: bool = True


class ToolSettings(BaseModel):
    enabled: list[str] = Field(
        default_factory=lambda: [
            "read_url",
            "web_search",
            "read_file",
            "view_image",
            "expand_forward",
            "search_history",
            "get_messages_around",
            "lookup_glossary",
            "get_profile",
            "render_mermaid",
        ]
    )
    read_url_max_chars: int = 16000
    browser_fallback: bool = True
    file_max_mb: int = 30
    file_max_pages: int = 40
    search_results: int = 6
    history_top_k: int = 12
    request_limit: int = 12
    tool_calls_limit: int = 16


class MemorySettings(BaseModel):
    enabled: bool = True
    chunk_gap_minutes: int = Field(default=20, description="消息间隔超过这个时间就切块")
    chunk_max_messages: int = 60
    chunk_max_chars: int = 2400
    topic_min_new_messages: int = Field(default=120, description="新增消息达到这个数量触发话题分段")
    topic_window_messages: int = 400
    topic_close_after_minutes: int = 120
    digest_hour: int = Field(default=4, ge=0, le=23, description="每日摘要生成时间（Asia/Shanghai）")
    profile_min_messages: int = 15
    glossary_min_count: int = 6
    glossary_max_candidates: int = 30
    backfill_messages: int = Field(default=3000, description="群加入白名单时回填的历史消息数")


class PrivacySettings(BaseModel):
    log_message_content: bool = False
    capture_llm_content: bool = Field(default=False, description="trace 里记录提示词和回复原文（排障用）")


class Settings(BaseModel):
    persona: PersonaSettings = Field(default_factory=PersonaSettings)
    interaction: InteractionSettings = Field(default_factory=InteractionSettings)
    moderation: ModerationSettings = Field(default_factory=ModerationSettings)
    ratelimit: RateLimitSettings = Field(default_factory=RateLimitSettings)
    context: ContextSettings = Field(default_factory=ContextSettings)
    tools: ToolSettings = Field(default_factory=ToolSettings)
    memory: MemorySettings = Field(default_factory=MemorySettings)
    privacy: PrivacySettings = Field(default_factory=PrivacySettings)


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def effective_settings(global_doc: dict[str, Any], group_doc: dict[str, Any] | None) -> Settings:
    merged = deep_merge(Settings().model_dump(mode="json"), global_doc)
    if group_doc:
        merged = deep_merge(merged, group_doc)
    return Settings.model_validate(merged)
