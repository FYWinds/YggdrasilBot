"""A tool's ``RunContext`` as an agent run hands it over."""

from typing import Any, cast

from pydantic_ai import RunContext
from pydantic_ai.models.test import TestModel
from pydantic_ai.usage import RunUsage

from yggdrasil.agent.deps import Deps, Handles
from yggdrasil.napcat.api import NapCatAPI
from yggdrasil.settings import Settings


def run_ctx(
    *,
    api: NapCatAPI | None = None,
    group_id: int | None = 1,
    user_id: int = 2,
    settings: Settings | None = None,
    tool_call_id: str | None = None,
    **deps: Any,
) -> RunContext[Deps]:
    """The context of a chat run in ``group_id`` asked for by ``user_id``, for calling a tool directly. ``deps`` sets
    any other ``Deps`` field (``self_id=``, ``trigger_message_id=``, ...); without ``api`` nothing may call QQ."""
    run = Deps(cast(NapCatAPI, api), group_id, user_id, settings or Settings(), Handles(), "chat", **deps)
    return RunContext(deps=run, model=TestModel(), usage=RunUsage(), tool_call_id=tool_call_id)
