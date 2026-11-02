"""QQ faked at the OneBot call: the real ``NapCatAPI`` over a scripted ``call_api``, and group message events."""

import inspect
from collections.abc import Mapping
from typing import Any, cast

from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent, Message

from yggdrasil.napcat.api import NapCatAPI


class ScriptedBot:
    """NoneBot's OneBot ``Bot`` as far as the bot uses it (``self_id``, ``call_api``). Each action is answered from
    ``answers``: OneBot data or a model to return, an exception to raise, or a function of the call's params that
    returns (or raises) one. Every call is kept in ``calls``; an action without an answer fails."""

    def __init__(self, answers: Mapping[str, Any] | None = None, *, self_id: int = 10000) -> None:
        self.self_id = str(self_id)
        self.answers: dict[str, Any] = dict(answers or {})
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def call_api(self, api: str, **data: Any) -> Any:
        self.calls.append((api, data))
        if api not in self.answers:
            raise NotImplementedError(f"no scripted answer for {api}")
        answer = self.answers[api]
        if callable(answer):
            answer = answer(**data)
            if inspect.isawaitable(answer):
                answer = await answer
        if isinstance(answer, BaseException):
            raise answer
        return answer

    def called(self, action: str) -> list[dict[str, Any]]:
        """The params of every ``action`` call, oldest first (``_timeout`` is the reply timeout)."""
        return [data for api, data in self.calls if api == action]


class FakeNapCat(NapCatAPI):
    """``NapCatAPI`` over a ``ScriptedBot``: the methods are the real ones, so the fake can't drift from them."""

    def __init__(self, answers: Mapping[str, Any] | None = None, *, self_id: int = 10000) -> None:
        self.script = ScriptedBot(answers, self_id=self_id)
        super().__init__(cast(Bot, self.script))

    def called(self, action: str) -> list[dict[str, Any]]:
        return self.script.called(action)


def group_message(
    message: Message | str,
    *,
    group_id: int,
    user_id: int,
    self_id: int,
    message_id: int = 1,
    card: str = "",
    nickname: str | None = None,
    to_me: bool = False,
    reply: dict[str, Any] | None = None,
) -> GroupMessageEvent:
    """A group message as the OneBot adapter delivers it."""
    msg = Message(message)
    sender: dict[str, Any] = {"user_id": user_id, "card": card}
    if nickname is not None:
        sender["nickname"] = nickname
    return GroupMessageEvent.model_validate(
        {
            "time": 1760000000,
            "self_id": self_id,
            "post_type": "message",
            "sub_type": "normal",
            "user_id": user_id,
            "message_type": "group",
            "message_id": message_id,
            "message": msg,
            "original_message": msg,
            "raw_message": "",
            "font": 0,
            "sender": sender,
            "to_me": to_me,
            "group_id": group_id,
            "reply": reply,
        }
    )
