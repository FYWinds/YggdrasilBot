"""Typed NapCat (OneBot v11 + extensions) payloads. Lenient: unknown fields are kept."""

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class _Model(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)


class Segment(_Model):
    type: str
    data: dict[str, Any] = Field(default_factory=dict)


class Sender(_Model):
    user_id: int | None = None
    nickname: str = ""
    card: str = ""
    role: str | None = None


class MessageRecord(_Model):
    """A message as returned by get_msg / get_group_msg_history / get_forward_msg."""

    message_id: int | None = None
    message_seq: int | None = None
    real_id: int | None = None
    time: int = 0
    user_id: int | None = None
    group_id: int | None = None
    sender: Sender = Field(default_factory=Sender)
    message: list[Segment] | str = Field(default_factory=list)
    raw_message: str = ""

    @property
    def segments(self) -> list[Segment]:
        if isinstance(self.message, str):
            return [Segment(type="text", data={"text": self.message})]
        return self.message


class SendResult(_Model):
    message_id: int


class ForwardResult(_Model):
    message_id: int
    res_id: str | None = None
    forward_id: str | None = None


class ForwardMsg(_Model):
    messages: list[MessageRecord] = Field(default_factory=list)


class MsgHistory(_Model):
    messages: list[MessageRecord] = Field(default_factory=list)


class FileInfo(_Model):
    file: str | None = None  # local path on the NapCat host
    url: str | None = None
    file_size: int | str | None = None
    file_name: str | None = None
    base64: str | None = None


class FileUrl(_Model):
    url: str


class LoginInfo(_Model):
    user_id: int
    nickname: str


class GroupInfo(_Model):
    group_id: int
    group_name: str = ""
    member_count: int | None = None


class MemberInfo(_Model):
    group_id: int
    user_id: int
    nickname: str = ""
    card: str = ""
    role: str = "member"


class OcrText(_Model):
    text: str


class OcrResult(_Model):
    texts: list[OcrText] = Field(default_factory=list)


class VersionInfo(_Model):
    app_name: str = ""
    app_version: str = ""
    protocol_version: str = ""
