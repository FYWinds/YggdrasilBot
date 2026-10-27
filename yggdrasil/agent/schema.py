from typing import Literal

from pydantic import BaseModel, Field


class Section(BaseModel):
    # No title: the forward card preview shows the first lines of each node, so a heading wastes it.
    content: str = Field(description="正文，纯文本，不要 markdown，不要加小标题；按意思分段，段落之间空一行")


class SourceRef(BaseModel):
    label: str = Field(description="来源名称，如网页标题、文件名、#消息id")
    url: str | None = None


class BotReply(BaseModel):
    """The structured answer of the chat / tldr agents."""

    mode: Literal["forward", "silent", "done"] = Field(
        description="forward：查证后用合并转发作答；silent：不回复（没有实际问题、不是在跟你说话或不该接的请求）；"
        "done：已经用操作类工具完成了请求（比如撤回），不再发消息"
    )
    title: str | None = Field(default=None, description="mode=forward 时这次回复的主题（≤20 字）")
    sections: list[Section] = Field(default_factory=list, description="mode=forward 时的各节")
    sources: list[SourceRef] = Field(default_factory=list, description="引用过的来源")
    silent_reason: str | None = Field(
        default=None, description="mode=silent 时一句话说明为什么不回（只记日志，不发出去）"
    )

    def full_text(self) -> str:
        return "\n\n".join(s.content for s in self.sections if s.content.strip())


class OpsReply(BaseModel):
    """The structured answer of the ops agent (``yggdrasil.ops.agent``)."""

    mode: Literal["done", "reply", "handoff"] = Field(
        description="done：已用工具完成操作，不发消息；reply：回一段简短纯文本（查询结果、失败原因、需要澄清时）；"
        "handoff：不是管理操作，交给普通答疑（只在群聊里用）"
    )
    text: str | None = Field(default=None, description="mode=reply 时发出的文字，纯文本，简短")
