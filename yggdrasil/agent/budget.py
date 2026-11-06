"""A run's tool budget as a soft limit: once it is spent the function tools are withdrawn, so the model has to answer
from what it already read instead of the run failing on ``UsageLimitExceeded`` after all the reading was done."""

from dataclasses import dataclass
from typing import Any

from pydantic_ai import RunContext
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.tools import ToolDefinition

from yggdrasil.core.log import LOGGER


@dataclass
class ToolBudget(AbstractCapability[Any]):
    """No function tools once ``tool_calls`` calls were made, or for the last of ``requests`` model requests.

    ``requests`` must also be the run's hard ``request_limit``: the request it reserves is the one the answer is
    written in. One batch of parallel calls may overshoot ``tool_calls``; the check runs before each request."""

    requests: int
    tool_calls: int

    async def prepare_tools(self, ctx: RunContext[Any], tool_defs: list[ToolDefinition]) -> list[ToolDefinition]:
        if tool_defs and (ctx.usage.tool_calls >= self.tool_calls or ctx.usage.requests >= self.requests - 1):
            u = ctx.usage
            LOGGER.info("tool budget spent ({} calls, {} requests): answering without tools", u.tool_calls, u.requests)
            return []
        return tool_defs
