"""A run's tool budget as a soft limit: calls past it return a notice instead of running, so the model answers from
what it already read instead of the run failing on ``UsageLimitExceeded`` after all the reading was done."""

from dataclasses import dataclass, field, replace
from typing import Any

from pydantic_ai import RunContext
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.capabilities.abstract import ValidatedToolArgs, WrapToolExecuteHandler
from pydantic_ai.messages import ToolCallPart
from pydantic_ai.tools import ToolDefinition

from yggdrasil.core.log import LOGGER

SPENT = (
    "这次请求的工具调用次数已经用完，这个调用没有执行。直接根据已经读到的内容作答，"
    "没来得及查证的地方写明没查完，不要写成查过的结论。"
)


@dataclass
class ToolBudget(AbstractCapability[Any]):
    """Function tool calls past the first ``tool_calls``, and those whose results would go into the last of
    ``requests`` model requests, return ``SPENT`` without running. A batch of parallel calls is counted call by call.
    The last request gets no function tools at all, so the model writes its answer there.

    ``requests`` must also be the run's hard ``request_limit``. Must come before the capabilities whose tool
    wrappers should not see a skipped call (accounting) and after the transcript recorder."""

    requests: int
    tool_calls: int
    _calls: int = field(default=0, init=False)

    async def for_run(self, ctx: RunContext[Any]) -> "ToolBudget":
        return replace(self)

    def _last_request_next(self, ctx: RunContext[Any]) -> bool:
        return ctx.usage.requests >= self.requests - 1

    async def prepare_tools(self, ctx: RunContext[Any], tool_defs: list[ToolDefinition]) -> list[ToolDefinition]:
        if tool_defs and self._last_request_next(ctx):
            LOGGER.info("last request of {}: answering without tools", self.requests)
            return []
        return tool_defs

    async def wrap_tool_execute(
        self,
        ctx: RunContext[Any],
        *,
        call: ToolCallPart,
        tool_def: ToolDefinition,
        args: ValidatedToolArgs,
        handler: WrapToolExecuteHandler,
    ) -> Any:
        if tool_def.kind != "function":
            return await handler(args)
        self._calls += 1
        if self._calls > self.tool_calls or self._last_request_next(ctx):
            n, requests = self._calls, ctx.usage.requests
            LOGGER.info("tool budget spent ({} calls, {} requests): {} not run", n, requests, call.tool_name)
            return SPENT
        return await handler(args)
