from typing import Literal

from pydantic import BaseModel, Field


class Section(BaseModel):
    title: str | None = Field(default=None, description="节标题（可选，短）")
    content: str = Field(description="正文，纯文本，不要 markdown")


class SourceRef(BaseModel):
    label: str = Field(description="来源名称，如网页标题、文件名、#消息id")
    url: str | None = None


class BotReply(BaseModel):
    """The structured answer of the chat / tldr agents."""

    mode: Literal["messages", "forward", "silent"] = Field(
        description=(
            "messages：像群友一样发 1–3 条短消息；forward：内容确实很长或结构化时用合并转发；"
            "silent：不回复（不是在跟你说话、无意义或不该接的请求），什么都不发"
        )
    )
    messages: list[str] = Field(
        default_factory=list,
        description="mode=messages 时依次发送的短消息，每条一句话；mode=forward 时可选一条在转发前发送的引导语",
    )
    title: str | None = Field(default=None, description="mode=forward 时合并转发卡片的标题（≤20 字）")
    sections: list[Section] = Field(default_factory=list, description="mode=forward 时的各节")
    sources: list[SourceRef] = Field(default_factory=list, description="引用过的来源")
    silent_reason: str | None = Field(
        default=None, description="mode=silent 时一句话说明为什么不回（只记日志，不发出去）"
    )

    def full_text(self) -> str:
        parts = list(self.messages)
        for s in self.sections:
            parts.append(f"{s.title or ''}\n{s.content}")
        return "\n\n".join(p for p in parts if p.strip())


class AckDecision(BaseModel):
    kind: Literal["text", "sticker", "none"] = Field(description="快速响应的形式")
    text: str | None = Field(default=None, description="kind=text 时的一句短话（≤20 字）")
    sticker_id: int | None = Field(default=None, description="kind=sticker 时从候选表情包里选的 id")


class ReactDecision(BaseModel):
    emoji_id: str = Field(description="从候选表情 id 里选一个")
