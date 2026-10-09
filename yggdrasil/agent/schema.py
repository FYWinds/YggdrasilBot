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

    mode: Literal["forward", "silent"] = Field(
        description="forward：查证后用合并转发作答；silent：不回复（没有实际问题、不是在跟你说话或不该接的请求）"
    )
    title: str | None = Field(default=None, description="mode=forward 时合并转发卡片的标题（≤20 字）")
    sections: list[Section] = Field(default_factory=list, description="mode=forward 时的各节")
    sources: list[SourceRef] = Field(default_factory=list, description="引用过的来源")
    silent_reason: str | None = Field(
        default=None, description="mode=silent 时一句话说明为什么不回（只记日志，不发出去）"
    )

    def full_text(self) -> str:
        parts: list[str] = []
        for s in self.sections:
            parts.append(f"{s.title or ''}\n{s.content}")
        return "\n\n".join(p for p in parts if p.strip())
