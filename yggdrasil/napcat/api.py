"""Typed facade over NapCat's OneBot v11 actions (incl. extensions).

Same shape as ``nonebot.adapters.onebot.v11.bot.pyi`` (keyword-only), but replies
are validated into pydantic models. Calls go through ``bot.call_api`` so the
telemetry hooks in :mod:`yggdrasil.napcat.hooks` see every one.
"""

from typing import Any

from nonebot.adapters.onebot.v11 import Bot
from pydantic import BaseModel, TypeAdapter

from yggdrasil.napcat.models import (
    FileInfo,
    FileUrl,
    ForwardMsg,
    ForwardResult,
    GroupInfo,
    LoginInfo,
    MemberInfo,
    MessageRecord,
    MsgHistory,
    OcrResult,
    SendResult,
    VersionInfo,
)

Segments = list[dict[str, Any]]


def text_seg(text: str) -> dict[str, Any]:
    return {"type": "text", "data": {"text": text}}


def reply_seg(message_id: int) -> dict[str, Any]:
    return {"type": "reply", "data": {"id": str(message_id)}}


def at_seg(user_id: int) -> dict[str, Any]:
    return {"type": "at", "data": {"qq": str(user_id)}}


def image_seg(file: str) -> dict[str, Any]:
    """``file``: http(s) URL, ``base64://...`` or ``file://`` path."""
    return {"type": "image", "data": {"file": file}}


def markdown_seg(content: str) -> dict[str, Any]:
    return {"type": "markdown", "data": {"content": content}}


def node_seg(user_id: int, nickname: str, content: Segments) -> dict[str, Any]:
    return {"type": "node", "data": {"user_id": str(user_id), "nickname": nickname, "content": content}}


_MODELS: dict[type, TypeAdapter[Any]] = {}


def _parse[T: BaseModel](model: type[T], value: Any) -> T:
    adapter = _MODELS.get(model)
    if adapter is None:
        adapter = _MODELS[model] = TypeAdapter(model)
    return adapter.validate_python(value)


class NapCatAPI:
    def __init__(self, bot: Bot) -> None:
        self.bot = bot

    async def _call(self, action: str, **params: Any) -> Any:
        return await self.bot.call_api(action, **{k: v for k, v in params.items() if v is not None})

    # --- identity / info -------------------------------------------------
    async def get_login_info(self) -> LoginInfo:
        return _parse(LoginInfo, await self._call("get_login_info"))

    async def get_version_info(self) -> VersionInfo:
        return _parse(VersionInfo, await self._call("get_version_info"))

    async def get_group_list(self) -> list[GroupInfo]:
        return [_parse(GroupInfo, g) for g in await self._call("get_group_list")]

    async def get_group_member_info(self, *, group_id: int, user_id: int, no_cache: bool = False) -> MemberInfo:
        return _parse(
            MemberInfo, await self._call("get_group_member_info", group_id=group_id, user_id=user_id, no_cache=no_cache)
        )

    # --- messages ---------------------------------------------------------
    # reply_timeout: seconds to wait for the reply instead of the adapter's api_timeout (images upload slowly).
    async def send_group_msg(
        self, *, group_id: int, message: Segments, reply_timeout: float | None = None
    ) -> SendResult:
        return _parse(
            SendResult, await self._call("send_group_msg", group_id=group_id, message=message, _timeout=reply_timeout)
        )

    async def send_private_msg(
        self, *, user_id: int, message: Segments, reply_timeout: float | None = None
    ) -> SendResult:
        return _parse(
            SendResult, await self._call("send_private_msg", user_id=user_id, message=message, _timeout=reply_timeout)
        )

    # No source/summary/prompt/news overrides: a preview that doesn't match the nodes trips QQ's risk
    # control ("risky link" warnings); NapCat derives the real one from the node contents.
    async def send_group_forward_msg(self, *, group_id: int, messages: Segments) -> ForwardResult:
        return _parse(ForwardResult, await self._call("send_group_forward_msg", group_id=group_id, messages=messages))

    async def send_private_forward_msg(self, *, user_id: int, messages: Segments) -> ForwardResult:
        return _parse(ForwardResult, await self._call("send_private_forward_msg", user_id=user_id, messages=messages))

    async def get_msg(self, *, message_id: int) -> MessageRecord:
        return _parse(MessageRecord, await self._call("get_msg", message_id=message_id))

    async def get_forward_msg(self, *, id: str) -> ForwardMsg:
        return _parse(ForwardMsg, await self._call("get_forward_msg", id=id))

    async def get_group_msg_history(
        self, *, group_id: int, count: int = 20, before_id: int | None = None
    ) -> MsgHistory:
        """A page of history: message ``before_id`` and older (the latest page when None).

        The anchor is a message id under both names: NapCat calls it ``message_seq`` (its seq is the
        short message id), SnowLuma ``message_id``. Both default to returning the anchor and older.
        """
        return _parse(
            MsgHistory,
            await self._call(
                "get_group_msg_history", group_id=group_id, count=count, message_seq=before_id, message_id=before_id
            ),
        )

    async def set_msg_emoji_like(self, *, message_id: int, emoji_id: str, set: bool = True) -> None:
        await self._call("set_msg_emoji_like", message_id=message_id, emoji_id=emoji_id, set=set)

    async def delete_msg(self, *, message_id: int) -> None:
        await self._call("delete_msg", message_id=message_id)

    # --- files / media ----------------------------------------------------
    async def get_file(self, *, file_id: str) -> FileInfo:
        return _parse(FileInfo, await self._call("get_file", file_id=file_id))

    async def get_image(self, *, file: str) -> FileInfo:
        return _parse(FileInfo, await self._call("get_image", file=file))

    async def get_group_file_url(self, *, group_id: int, file_id: str, busid: int | None = None) -> FileUrl:
        return _parse(FileUrl, await self._call("get_group_file_url", group_id=group_id, file_id=file_id, busid=busid))

    async def ocr_image(self, *, image: str) -> OcrResult:
        result = await self._call("ocr_image", image=image)
        if isinstance(result, list):  # some NapCat versions return the list directly
            result = {"texts": result}
        return _parse(OcrResult, result)
