import pytest
from pydantic_ai import Agent
from pydantic_ai.messages import ModelMessage, ModelResponse, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.usage import UsageLimits

from yggdrasil.agent.budget import ToolBudget
from yggdrasil.agent.schema import BotReply

ANSWER = ToolCallPart("final_result", {"mode": "forward", "title": "答", "sections": [{"content": "根据已读内容"}]})


async def _run(budget: ToolBudget, calls_per_step: int) -> tuple[BotReply, list[list[str]], int]:
    """A model that keeps reading while any function tool is offered; the tools offered on each request."""
    offered: list[list[str]] = []
    read: list[int] = []

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        offered.append([t.name for t in info.function_tools])
        if not info.function_tools:
            return ModelResponse(parts=[ANSWER])
        return ModelResponse(
            parts=[ToolCallPart("read_url", {"url": f"https://example.com/{i}"}) for i in range(calls_per_step)]
        )

    agent = Agent(FunctionModel(respond), output_type=BotReply, capabilities=[budget])

    @agent.tool_plain
    async def read_url(url: str) -> str:
        read.append(1)
        return "正文"

    result = await agent.run("问题", usage_limits=UsageLimits(request_limit=budget.requests))
    return result.output, offered, len(read)


@pytest.mark.parametrize(
    ("budget", "calls_per_step", "requests", "reads"),
    [
        # 2 + 2 calls reach the cap of 3 (one batch may overshoot); the third request answers.
        (ToolBudget(requests=12, tool_calls=3), 2, 3, 4),
        # The last allowed request is kept for the answer although calls are left.
        (ToolBudget(requests=3, tool_calls=16), 1, 3, 2),
    ],
)
async def test_a_spent_budget_withdraws_the_tools_and_the_run_still_answers(
    budget: ToolBudget, calls_per_step: int, requests: int, reads: int
) -> None:
    reply, offered, read = await _run(budget, calls_per_step)

    assert reply.sections[0].content == "根据已读内容"
    assert offered == [["read_url"]] * (requests - 1) + [[]]
    assert read == reads
