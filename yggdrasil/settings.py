"""Customizable behaviour, stored as JSON in the database.

The global document (``settings`` row ``global``) is validated by :class:`Settings`;
a group's ``groups.settings`` is deep-merged over it. The dashboard renders forms
from ``Settings.model_json_schema()``, so every field here is editable.
"""

from typing import Any, Literal, Self

from pydantic import BaseModel, Field, model_validator


class PersonaSettings(BaseModel):
    name: str = Field(default="尤克特拉希尔", description="bot 的名字，出现在提示词里")
    aliases: list[str] = Field(
        default_factory=lambda: ["ygg", "世界树"], description="点名触发词（name_mention 规则使用）"
    )
    description: str = Field(
        default="一个懂行、靠谱、有点毒舌但心软的老群友。说话轻松口语化，偶尔吐槽，但从不敷衍正经问题。",
        description="人设：性格、说话风格、口癖等，会写进所有对话类提示词",
    )


class InteractionSettings(BaseModel):
    # Status reactions on the trigger message; added only, never removed (frequent removals look like
    # automation to QQ's risk control). Empty disables one.
    received_emoji: str = Field(default="424", description="收到请求后立刻贴的表情")
    slow_tool_emoji: str = Field(default="314", description="第一次调用耗时工具（见 tools.slow_tools）时贴的表情")
    done_emoji: str = Field(default="127881", description="回复完成时贴的表情（🎉）")
    silent_emoji: str = Field(default="479", description="bot 决定不回复时贴的表情")
    review_emoji: str = Field(default="10068", description="输入被拦截、输出进入审核或被拦截时贴的表情（❔）")
    action_emoji: str = Field(default="478", description="按指令完成了操作（撤回、改配置等）且不需要回复时贴的表情")
    forward_max_nodes: int = Field(default=12, ge=1, le=60)
    max_sections: int = Field(default=2, ge=1, le=10, description="一次回复最多几节，多出的节合并进最后一节")
    node_max_chars: int = Field(default=1500, ge=200)
    # Asked for at the end of the prompt, where models heed it more than in the system prompt; they
    # still overshoot Chinese character counts by about 1.5-2x.
    tldr_max_chars: int = Field(
        default=200, ge=50, le=2000, description="tldr 正文的目标字数（模型通常会写超 1.5–2 倍）"
    )
    tldr_focus_max_chars: int = Field(
        default=150, ge=50, le=2000, description="tldr 后面带了关注点时，正文的目标字数（只回答关注点）"
    )
    forward_render: Literal["plain", "nested_markdown"] = Field(
        default="plain", description="plain：去掉 markdown 语法；nested_markdown：双层合并转发里发 markdown 段"
    )
    failure_emoji: str = Field(default="5", description="出错时贴的表情")
    ratelimited_emoji: str = Field(default="38", description="被限流或排队超时时贴的表情")
    quote_reply: Literal["always", "when_busy", "never"] = Field(
        default="when_busy",
        description="回复时是否引用触发消息：always 总是；when_busy 触发消息之后群里已有别人发言才引用；never 从不",
    )
    metadata_node: bool = True
    metadata_fields: list[Literal["model", "tokens", "tools", "latency", "moderation", "trace"]] = Field(
        default_factory=lambda: ["model", "tokens", "tools", "latency", "moderation", "trace"]
    )


class ModerationCategoryRule(BaseModel):
    # Score = P(Unsafe) + 0.5 * P(Controversial): a confident "Controversial" sits near 0.5, so 0.8
    # only acts on fairly confident "Unsafe" verdicts and trusts the main model with gray areas.
    threshold: float = Field(default=0.8, ge=0, le=1)
    action: Literal["pass", "review", "drop"] = "review"


class ModerationSettings(BaseModel):
    enabled: bool = True
    check_input: bool = True
    check_output: bool = True
    default_rule: ModerationCategoryRule = Field(default_factory=ModerationCategoryRule)
    category_rules: dict[str, ModerationCategoryRule] = Field(
        default_factory=lambda: {
            "Politically Sensitive Topics": ModerationCategoryRule(threshold=0.8, action="review"),
            "Sexual Content or Sexual Acts": ModerationCategoryRule(threshold=0.8, action="review"),
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
    inline_images: int = Field(
        default=4,
        ge=0,
        le=10,
        description="被引用消息和触发消息里的图片，最多直接附几张给能看图的模型（0：不附，改由模型调用 view_image）",
    )


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
            "recent_chat",
            "lookup_glossary",
            "get_profile",
            "render_mermaid",
            "render_markdown",
            "render_html",
            "sandbox",
        ],
        description="启用的工具；sandbox 表示整套工作区工具（命令行、文件、send_image、serve 等，只在群聊 chat 生效）",
    )
    slow_tools: list[str] = Field(
        default_factory=lambda: [
            "read_url",
            "web_search",
            "read_file",
            "view_image",
            "expand_forward",
            "render_mermaid",
            "render_markdown",
            "render_html",
            "run_command",
            "sandbox_put",
            "send_image",
            "serve",
            "reset_workspace",
        ],
        description="耗时工具：第一次调用时贴 interaction.slow_tool_emoji",
    )
    read_url_max_chars: int = 16000
    browser_fallback: bool = True
    file_max_mb: int = 30
    file_max_pages: int = 40
    search_results: int = 6
    history_top_k: int = 12
    request_limit: int = 12
    tool_calls_limit: int = 16
    sandbox_enabled: bool = Field(
        default=True, description="工作区总开关（只看全局设置，群设置里改它无效）；关掉后所有群都没有工作区工具"
    )
    sandbox_command_timeout_s: int = Field(default=60, ge=1, le=300, description="工作区单条命令的默认超时（秒）")
    sandbox_command_max_s: int = Field(
        default=300, ge=1, le=1800, description="工作区单条命令最长允许的超时（秒），模型指定更长的会被截到这里"
    )
    sandbox_invocation_s: int = Field(
        default=600, ge=30, le=3600, description="一次请求里工作区命令累计最多运行多久（秒）"
    )
    sandbox_max_commands: int = Field(default=40, ge=1, le=200, description="一次请求里最多执行多少条工作区命令")
    sandbox_idle_min: int = Field(
        default=30, ge=5, le=1440, description="工作区闲置多久（分钟）后回收，回收后文件全部丢失"
    )
    sandbox_max_age_min: int = Field(
        default=240, ge=10, le=10080, description="工作区最长存活时间（分钟），到期后下次使用会重建"
    )
    sandbox_max_workspaces: int = Field(default=4, ge=1, le=50, description="全局同时存在的工作区上限")
    sandbox_max_links: int = Field(default=3, ge=0, le=20, description="每个工作区同时公开的链接（serve）上限")
    sandbox_request_limit: int = Field(
        default=40, ge=1, le=200, description="有工作区工具的请求的模型调用次数上限（取代 request_limit）"
    )
    sandbox_tool_calls_limit: int = Field(
        default=60, ge=1, le=400, description="有工作区工具的请求的工具调用次数上限（取代 tool_calls_limit）"
    )

    @model_validator(mode="after")
    def _sandbox_timeouts(self) -> Self:
        if self.sandbox_idle_min * 60 <= self.sandbox_invocation_s:
            raise ValueError("sandbox_idle_min 换算成秒必须大于 sandbox_invocation_s，否则工作区可能在请求进行中被回收")
        if self.sandbox_command_timeout_s > self.sandbox_command_max_s:
            raise ValueError("sandbox_command_timeout_s 不能大于 sandbox_command_max_s")
        return self


class OpsSettings(BaseModel):
    napcat_write_actions: list[str] = Field(
        default_factory=lambda: [
            "delete_msg",
            "set_group_ban",
            "set_group_whole_ban",
            "set_group_kick",
            "set_group_card",
            "set_essence_msg",
            "delete_essence_msg",
            "_send_group_notice",
            "send_group_msg",
            "send_private_msg",
        ],
        description="管理助手（超级管理员）可以调用的会改东西的 NapCat 接口；get_ 开头的只读接口不用列。"
        "退群、删好友、改账号资料、处理加群加好友申请、重启这类接口始终禁止",
    )


class MemorySettings(BaseModel):
    enabled: bool = True
    chunk_gap_minutes: int = Field(default=20, description="消息间隔超过这个时间就切块")
    chunk_max_messages: int = 60
    chunk_max_chars: int = 2400
    topic_min_new_messages: int = Field(default=120, description="新增消息达到这个数量触发话题分段")
    topic_window_messages: int = 400
    topic_close_after_minutes: int = 120
    topic_archive_below_messages: int = Field(
        default=20,
        ge=0,
        description="话题结束时，消息数少于这个值、且参与人数少于下一项的自动归档。"
        "归档的话题只在控制台可见，agent 搜不到（0：不归档）",
    )
    topic_archive_below_participants: int = Field(
        default=3, ge=0, description="归档条件：参与人数少于这个值（和消息数条件同时满足才归档）"
    )
    digest_hour: int = Field(default=4, ge=0, le=23, description="每日摘要生成时间（Asia/Shanghai）")
    profile_min_messages: int = 15
    glossary_min_count: int = 6
    glossary_max_candidates: int = 30
    backfill_messages: int = Field(default=3000, description="群加入白名单时回填的历史消息数")


class PrivacySettings(BaseModel):
    log_message_content: bool = False
    capture_llm_content: bool = Field(default=False, description="trace 里记录提示词和回复原文（排障用）")


class EventSettings(BaseModel):
    """Requests and notices (only the global document applies)."""

    auto_accept_superuser_invites: bool = Field(default=True, description="超级管理员邀请 bot 进群时自动同意")
    auto_enable_invited_groups: bool = Field(default=True, description="自动同意的邀请，同时把群加入白名单并回填历史")
    notify_invites: bool = Field(default=True, description="其他人的加群邀请私聊通知超级管理员")
    auto_accept_superuser_friends: bool = Field(default=True, description="超级管理员加 bot 好友时自动同意")
    notify_friend_requests: bool = Field(default=True, description="其他人的好友申请私聊通知超级管理员")
    notify_join_requests: bool = Field(
        default=False, description="有人申请加入 bot 当管理的群时私聊通知（只记录，不代为处理）"
    )
    disable_group_when_kicked: bool = Field(default=True, description="bot 被踢出群时自动停用该群")
    notify_kicked: bool = Field(default=True, description="bot 被踢出群时私聊通知超级管理员")
    notify_banned: bool = Field(default=True, description="bot 在群里被禁言时私聊通知超级管理员")
    request_expire_days: int = Field(default=7, ge=1, description="待处理的申请超过这个天数标为过期")
    retention_days: int = Field(default=90, ge=7, description="事件记录保留天数")


class Settings(BaseModel):
    persona: PersonaSettings = Field(default_factory=PersonaSettings)
    interaction: InteractionSettings = Field(default_factory=InteractionSettings)
    moderation: ModerationSettings = Field(default_factory=ModerationSettings)
    ratelimit: RateLimitSettings = Field(default_factory=RateLimitSettings)
    context: ContextSettings = Field(default_factory=ContextSettings)
    tools: ToolSettings = Field(default_factory=ToolSettings)
    memory: MemorySettings = Field(default_factory=MemorySettings)
    privacy: PrivacySettings = Field(default_factory=PrivacySettings)
    ops: OpsSettings = Field(default_factory=OpsSettings)
    events: EventSettings = Field(default_factory=EventSettings)


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
