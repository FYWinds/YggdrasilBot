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
    # automation to QQ's risk control). QQ shows the requester a gray tip for each one, so a sent answer is its own
    # "done": result reactions mark only outcomes without a message. Empty disables one.
    received_emoji: str = Field(default="424", description="收到请求后立刻贴的表情")
    slow_emoji: str = Field(default="314", description="请求跑了 slow_after_s 秒还没有结果时贴的表情")
    slow_after_s: int = Field(default=30, ge=5, le=600, description="请求跑了多少秒还没有结果就贴 slow_emoji")
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
    failure_emoji: str = Field(default="5", description="出错时贴的表情")
    refusal_notice: bool = Field(
        default=True,
        description="模型因内容限制拒绝回答时，发一条消息说明原因（提供商给的分类和说明）；关掉则只贴 failure_emoji",
    )
    ratelimited_emoji: str = Field(default="38", description="被限流或排队超时时贴的表情")
    quote_reply: Literal["always", "when_busy", "never"] = Field(
        default="when_busy",
        description="回复时是否引用触发消息：always 总是；when_busy 触发消息之后群里已有别人发言才引用；never 从不",
    )
    metadata_node: bool = True
    metadata_fields: list[Literal["model", "tokens", "tools", "latency", "moderation", "trace"]] = Field(
        default_factory=lambda: ["model", "tokens", "tools", "latency", "moderation", "trace"]
    )
    ignore_users: list[int] = Field(
        default_factory=list,
        description="不响应这些 QQ 号（比如群里别的 bot）：消息照常记录，但不会触发任何指令。"
        "群名片或昵称里带 [bot] 的账号和 QQ 官方机器人自动忽略，不用列；超级管理员不受影响",
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
    input_action_on_hit: Literal["drop", "pass"] = Field(default="drop", description="输入超阈值时：不响应或放行")
    notify_admins_on_input_block: bool = False
    notify_group_on_review: bool = Field(default=True, description="进入审核时群里先回一句（模型生成）")
    review_expire_minutes: int = Field(default=120, ge=5)
    disable_unsafe_stickers: bool = Field(
        default=False,
        description="表情包打标时被审核判为有风险的直接禁用；关闭时只做标记（全局设置，按群覆盖无效）",
    )


class RateLimitSettings(BaseModel):
    user_per_minute: int = 4
    user_per_day: int = 80
    group_per_minute: int = 20
    group_daily_tokens: int = 3_000_000
    max_concurrent_per_group: int = Field(default=4, ge=1, description="每个群同时运行的请求数，超出排队")
    max_concurrent_per_user: int = Field(default=2, ge=1, description="每个人同时运行的请求数，超出排队")
    max_queued_per_user: int = Field(default=3, ge=0, description="每个人最多排队几个请求，再多就丢弃")
    queue_timeout_s: float = Field(default=180, gt=0, description="排队超过这个时间就放弃")
    repeat_window_s: int = Field(
        default=300,
        ge=0,
        description="同一目标的 tldr（回复同一条消息，或发同一个链接/同一段文字，关注点也相同）在这段时间内只做一次，"
        "重复的请求只贴 interaction.silent_emoji（超级管理员不受限；0：不去重）",
    )


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
    continue_minutes: int = Field(
        default=60,
        ge=0,
        description="群友回复 bot 的回答（chat 或 tldr）时，这么多分钟内的回答接着原来的对话继续：模型看得到之前查过的"
        "内容和说过的话，提示词只补上中间新增的群聊和这条请求。超过时间、对话太长或关闭了 privacy.capture_llm_content "
        "时重新带完整上下文（0：不续接）",
    )


class ToolSettings(BaseModel):
    enabled: list[str] = Field(
        default_factory=lambda: [
            "read_url",
            "web_search",
            "read_x",
            "search_x",
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
        description="启用的工具；sandbox 表示整套工作区工具（sh、py、文件读改、后台任务等，只在群聊 chat 生效）；"
        "read_x 读 X 帖子（先走免费的 FxTwitter，读不到再用 X API）；"
        "search_x 搜 X 上的动态（优先用搜索源里的 Grok，没有或失败时用 X API，都按条计费）；"
        "generate_image 画图（需要配置 image 路由，按张计费，默认不开）",
    )
    skills: list[str] = Field(
        default_factory=list,
        description="按需加载的技能（只在群聊 chat 生效）：平时只在提示词里占一行目录，模型需要时才加载它的工具和说明。"
        "qq 是 QQ 群的信息和操作：成员资料、群和成员头像、群公告和精华消息、群文件（列出、读取、把工作区里的文件上传到"
        "群文件）、戳一戳请求者",
    )
    slow_tools: list[str] = Field(
        default_factory=lambda: [
            "read_url",
            "web_search",
            "read_x",
            "search_x",
            "read_file",
            "view_image",
            "expand_forward",
            "render_mermaid",
            "render_markdown",
            "render_html",
            "generate_image",
            "sh",
            "py",
            "sandbox_put",
            "reset_workspace",
            "upload_file",
        ],
        description="耗时工具：回复末尾的元数据里显示它们的累计耗时",
    )
    read_url_max_chars: int = 16000
    browser_fallback: bool = True
    remote_fetch: bool = Field(
        default=True,
        description="read_url 在本地抓取和浏览器都读不到（或只拿到空壳）时，"
        "交给搜索源里优先级最高的 DeepSeek 远程抓取；按成功次数计费，计入该搜索源的月度额度",
    )
    file_max_mb: int = 30
    file_max_pages: int = 40
    search_results: int = 6
    history_top_k: int = 12
    request_limit: int = 12
    tool_calls_limit: int = 16
    sandbox_enabled: bool = Field(
        default=True, description="工作区总开关（只看全局设置，群设置里改它无效）；关掉后所有群都没有工作区工具"
    )
    sandbox_command_timeout_s: int = Field(
        default=60, ge=1, le=300, description="工作区 sh、py 单次调用的默认超时（秒）"
    )
    sandbox_command_max_s: int = Field(
        default=300, ge=1, le=1800, description="工作区 sh、py 单次调用最长允许的超时（秒），模型指定更长的会被截短"
    )
    sandbox_invocation_s: int = Field(
        default=600, ge=30, le=3600, description="一次请求里工作区 sh、py 累计最多运行多久（秒）"
    )
    sandbox_max_commands: int = Field(default=40, ge=1, le=200, description="一次请求里最多调用多少次 sh、py")
    sandbox_idle_min: int = Field(
        default=30, ge=5, le=1440, description="工作区闲置多久（分钟）后回收，回收后文件全部丢失"
    )
    sandbox_max_age_min: int = Field(
        default=240,
        ge=10,
        le=10080,
        description="工作区最长存活时间（分钟），到期后下次使用会重建；在里面开后台任务时会延长到够任务跑完",
    )
    sandbox_max_workspaces: int = Field(default=8, ge=1, le=50, description="全局同时存在的工作区（沙箱）上限")
    sandbox_max_workspaces_per_group: int = Field(
        default=3,
        ge=1,
        le=10,
        description="每群最多同时有几个工作区（一个话题一个）；满了再新建时回收最久没用、没有后台任务的那个",
    )
    sandbox_max_links: int = Field(default=3, ge=0, le=20, description="每个工作区同时公开的链接（publish）上限")
    sandbox_request_limit: int = Field(
        default=40, ge=1, le=200, description="有工作区工具的请求的模型调用次数上限（取代 request_limit）"
    )
    sandbox_tool_calls_limit: int = Field(
        default=60, ge=1, le=400, description="有工作区工具的请求的工具调用次数上限（取代 tool_calls_limit）"
    )
    sandbox_max_jobs: int = Field(
        default=2, ge=0, le=10, description="每群同时运行的后台任务上限（其中后台大任务最多 1 个）；0 关闭后台任务"
    )
    sandbox_job_command_min: int = Field(default=30, ge=1, le=600, description="后台命令最长运行时间（分钟）")
    sandbox_delegate: bool = Field(
        default=True, description="后台大任务（delegate）开关；关掉后仍有工作区和后台命令，只是不能委托给编程 agent"
    )
    sandbox_job_agent_min: int = Field(
        default=10,
        ge=1,
        le=240,
        description="后台大任务（delegate）最长运行时间（分钟）；到时让它停下并总结已完成的部分",
    )
    sandbox_job_max_input_tokens: int = Field(
        default=3_000_000, ge=10_000, description="每个后台大任务经中转最多消耗的输入 token"
    )
    sandbox_job_max_output_tokens: int = Field(
        default=100_000, ge=1_000, description="每个后台大任务经中转最多消耗的输出 token"
    )
    bridge_run_limit: int = Field(
        default=30,
        ge=0,
        le=500,
        description="一次请求里，工作区代码（sh、py）通过 ygg 调用 bot 工具、取聊天里的图片和文件"
        "最多多少次（不占模型自己的工具调用次数；0 关闭）",
    )
    bridge_job_limit: int = Field(
        default=120,
        ge=0,
        le=2000,
        description="每个后台任务（后台命令和后台大任务）通过 ygg 调用 bot 工具、取聊天里的图片和文件"
        "最多多少次（0 关闭）",
    )

    @model_validator(mode="after")
    def _sandbox_timeouts(self) -> Self:
        if self.sandbox_idle_min * 60 <= self.sandbox_invocation_s:
            raise ValueError("sandbox_idle_min 换算成秒必须大于 sandbox_invocation_s，否则工作区可能在请求进行中被回收")
        if self.sandbox_command_timeout_s > self.sandbox_command_max_s:
            raise ValueError("sandbox_command_timeout_s 不能大于 sandbox_command_max_s")
        return self


class OpsSettings(BaseModel):
    qq_write_actions: list[str] = Field(
        default_factory=lambda: [
            # OneBot 11
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
            # Milky
            "recall_group_message",
            "set_group_member_mute",
            "set_group_whole_mute",
            "kick_group_member",
            "set_group_member_card",
            "set_group_essence_message",
            "send_group_announcement",
            "send_group_message",
            "send_private_message",
        ],
        description="管理助手（超级管理员）通过 qq_api 可以调用的会改东西的 QQ 接口（OneBot 11 或 Milky 的接口名）；"
        "get_ 开头的只读接口不用列。退群、删好友、改账号资料、处理加群加好友申请、重启这类接口始终禁止",
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
    topic_archive_below_weight: float = Field(
        default=3.0,
        ge=0,
        description="整理任务每小时给已结束的话题打重要度（1–5），权重 = 重要度 × 规模系数（消息和参与人越多越大，"
        "50 条消息 9 个人约 1.6）。权重低于这个值的话题归档（0：不按权重归档）",
    )
    digest_hour: int = Field(default=4, ge=0, le=23, description="每日摘要生成时间（Asia/Shanghai）")
    profile_min_messages: int = 15
    fact_archive_below_importance: int = Field(
        default=3,
        ge=0,
        description="画像事实的重要度（1–5；5 是身份、技能、长期项目、稳定偏好，1 是一次性的饭局、心情、玩笑）"
        "低于这个值的归档：agent 和画像更新都看不到，控制台可恢复（0：不按重要度归档）",
    )
    facts_per_user: int = Field(
        default=40, ge=0, description="每人最多保留的有效事实，多出的按权重（含时间衰减）从低到高归档（0：不限）"
    )
    weight_half_life_days: int = Field(
        default=180,
        ge=1,
        description="排序用的时间衰减：画像事实和话题搜索结果按 权重 × 衰减 排序，"
        "过了这么多天的打 75 折，越旧越接近 5 折",
    )
    glossary_min_count: int = 6
    glossary_max_candidates: int = 30
    backfill_messages: int = Field(default=3000, description="群加入白名单时回填的历史消息数")


class StickerDropSettings(BaseModel):
    """Now and then a sticker: every few minutes, once enough new messages came in, a decision model (route
    ``sticker_pick``) judges whether the chat invites one and picks it from a weighted sample of the collected
    stickers."""

    enabled: bool = Field(
        default=False,
        description="群里聊得够多时，有机会挑一张收集到的表情包发到群里（需要配置 sticker_pick 路由）",
    )
    min_new_messages: int = Field(
        default=60, ge=1, description="上次判断之后群里新增这么多条消息（不算 bot 的）才再判断一次；每 5 分钟检查一次"
    )
    daily_max: int = Field(default=20, ge=0, description="每个群每天最多发几张（Asia/Shanghai 零点重置）")
    min_gap_minutes: int = Field(default=15, ge=0, description="两张之间至少间隔多少分钟")
    active_within_minutes: int = Field(
        default=5, ge=1, description="群里最后一条消息在这么多分钟以内才发（聊天已经冷了就不发）"
    )
    # Prod replay (main group, 2026-11-14..19, 1800-2800 messages a day): 27-40 checks a day passed
    # min_new_messages, the model put 84 % of them at 0.4 or more; with 0.7 that makes ~17 a day (15-19), a fifth of
    # the days reaching daily_max.
    min_fit: float = Field(
        default=0.4, ge=0, le=1, description="决策模型判断「最后几条适合用表情接话」的概率至少要这么高"
    )
    chance: float = Field(default=0.7, ge=0, le=1, description="通过以上条件后真正发出的概率")
    candidates: int = Field(
        default=50,
        ge=2,
        le=255,
        description="每次加权抽多少张表情给模型挑：用得多的权重高（按对数压缩，冷门的也抽得到），bot 最近发过的权重低",
    )
    group_only: bool = Field(
        default=False,
        description="只从本群发过的表情里挑，按本群的使用次数加权；关闭时用所有群收集到的表情，按总使用次数加权",
    )
    context_messages: int = Field(default=30, ge=5, le=100, description="给模型看的最近消息条数")


class PrivacySettings(BaseModel):
    log_message_content: bool = False
    capture_llm_content: bool = Field(
        default=True,
        description="保存提示词、思考、工具参数和结果、图片原文：控制台的对话记录和 trace 都用它；关闭后只记结构和用量",
    )


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
    sticker_drop: StickerDropSettings = Field(default_factory=StickerDropSettings)


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
