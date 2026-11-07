from pydantic_ai import Agent
from pydantic_ai.messages import ModelMessage, ModelRequest, ModelResponse, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.usage import UsageLimits

from yggdrasil.agent.budget import SPENT, ToolBudget
from yggdrasil.agent.schema import BotReply

ANSWER = ToolCallPart("final_result", {"mode": "forward", "title": "答", "sections": [{"content": "根据已读内容"}]})


class _Run:
    """A model that reads ``calls_per_step`` pages per request while function tools are offered; ``heeds`` makes it
    answer as soon as a call comes back unrun."""

    def __init__(self, budget: ToolBudget, calls_per_step: int, *, heeds: bool) -> None:
        self.budget, self.calls_per_step, self.heeds = budget, calls_per_step, heeds
        self.offered: list[list[str]] = []
        self.results: list[list[str]] = []  # the tool results each request carried
        self.reads = 0

    def respond(self, messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        self.offered.append([t.name for t in info.function_tools])
        last = messages[-1]
        parts = last.parts if isinstance(last, ModelRequest) else []
        results = [str(p.content) for p in parts if isinstance(p, ToolReturnPart)]
        if results:
            self.results.append(results)
        if not info.function_tools or (self.heeds and SPENT in results):
            return ModelResponse(parts=[ANSWER])
        step = len(self.offered)
        calls = [
            ToolCallPart("read_url", {"url": f"https://example.com/{step}/{i}"}) for i in range(self.calls_per_step)
        ]
        return ModelResponse(parts=calls)

    async def run(self) -> BotReply:
        agent = Agent(FunctionModel(self.respond), output_type=BotReply, capabilities=[self.budget])

        @agent.tool_plain
        async def read_url(url: str) -> str:  # pyright: ignore[reportUnusedParameter]
            self.reads += 1
            return "正文"

        result = await agent.run("问题", usage_limits=UsageLimits(request_limit=self.budget.requests))
        return result.output


async def test_a_parallel_batch_past_the_budget_runs_only_the_calls_within_it() -> None:
    run = _Run(ToolBudget(requests=12, tool_calls=3), 2, heeds=True)

    reply = await run.run()

    assert reply.sections[0].content == "根据已读内容"
    assert run.reads == 3
    assert run.results == [["正文", "正文"], ["正文", SPENT]]


async def test_a_model_that_keeps_calling_still_answers_in_the_last_request() -> None:
    run = _Run(ToolBudget(requests=4, tool_calls=3), 2, heeds=False)

    reply = await run.run()

    assert reply.sections[0].content == "根据已读内容"
    assert run.reads == 3
    assert run.results[-1] == [SPENT, SPENT]
    assert run.offered == [["read_url"]] * 3 + [[]]


async def test_calls_feeding_the_last_request_are_not_run_although_calls_are_left() -> None:
    run = _Run(ToolBudget(requests=3, tool_calls=16), 1, heeds=False)

    reply = await run.run()

    assert reply.sections[0].content == "根据已读内容"
    assert run.reads == 1
    assert run.results == [["正文"], [SPENT]]
    assert run.offered == [["read_url"], ["read_url"], []]


async def test_each_run_gets_a_fresh_count() -> None:
    budget = ToolBudget(requests=12, tool_calls=3)
    for _ in range(2):
        run = _Run(budget, 2, heeds=True)
        await run.run()
        assert run.reads == 3
