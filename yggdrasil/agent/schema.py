from typing import Literal

from pydantic import BaseModel, Field


class Section(BaseModel):
    title: str | None = Field(default=None, description="节标题（可选，短）")
    content: str = Field(description="正文，纯文本或轻量 markdown")


class SourceRef(BaseModel):
    label: str = Field(description="来源名称，如网页标题、文件名、#消息id")
    url: str | None = None


class BotReply(BaseModel):
    """The structured answer of the chat / tldr agents."""

    mode: Literal["inline", "forward"] = Field(
        description="inline：一两句话直接回复；forward：长内容，用合并转发分节展示"
    )
    inline_text: str | None = Field(default=None, description="mode=inline 时的回复文本")
    title: str | None = Field(default=None, description="mode=forward 时合并转发卡片的标题（≤20 字）")
    sections: list[Section] = Field(default_factory=list, description="mode=forward 时的各节")
    sources: list[SourceRef] = Field(default_factory=list, description="引用过的来源")

    def full_text(self) -> str:
        parts = [self.inline_text or ""]
        for s in self.sections:
            parts.append(f"{s.title or ''}\n{s.content}")
        return "\n\n".join(p for p in parts if p.strip())


class AckDecision(BaseModel):
    kind: Literal["text", "sticker", "none"] = Field(description="快速响应的形式")
    text: str | None = Field(default=None, description="kind=text 时的一句短话（≤20 字）")
    sticker_id: int | None = Field(default=None, description="kind=sticker 时从候选表情包里选的 id")


class ReactDecision(BaseModel):
    emoji_id: str = Field(description="从候选表情 id 里选一个")
