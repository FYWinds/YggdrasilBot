"""Structured LLM calls for the memory pipelines (tasks ``memory_*``)."""

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

from pydantic_ai import Agent
from pydantic_ai.models import Model
from pydantic_ai.settings import ModelSettings
from pydantic_ai.usage import UsageLimits

from yggdrasil.llm import registry

_DEFAULT_LIMITS = UsageLimits(request_limit=4)
_overrides: ContextVar[dict[str, Model] | None] = ContextVar("memory_model_overrides", default=None)


@contextmanager
def override_models(**models: Model) -> Iterator[None]:
    """Serve the given tasks (``memory_topic=FunctionModel(...)``) with fixed models; for tests and smoke runs."""
    token = _overrides.set({**(_overrides.get() or {}), **models})
    try:
        yield
    finally:
        _overrides.reset(token)


# Delimiter tags wrapping untrusted group content in the memory_* prompts.
DATA_TAGS = ("chat_log", "open_topics", "topics", "candidates", "terms", "profile")


def fence(text: str) -> str:
    """Neutralize closing delimiters inside untrusted text so it cannot end a data block early."""
    for tag in DATA_TAGS:
        text = text.replace(f"</{tag}", f"<\\/{tag}")
    return text


def available(task: str) -> bool:
    """Whether ``task`` can run (route configured or overridden)."""
    if task in (_overrides.get() or {}):
        return True
    try:
        registry.chat_model(task)
    except registry.TaskNotConfigured:
        return False
    return True


async def run_structured[T](task: str, output_type: type[T], prompt: str) -> T:
    """One agent run with structured output; usage goes to ``ygg.llm.tokens``.

    Raises :class:`registry.TaskNotConfigured` when the task has no route.
    """
    override = (_overrides.get() or {}).get(task)
    settings: ModelSettings | None
    if override is not None:
        model, settings, limits, label = override, None, _DEFAULT_LIMITS, f"override/{override.model_name}"
    else:
        resolved = registry.chat_model(task, default_limits=_DEFAULT_LIMITS)
        model, settings, limits, label = resolved.model, resolved.settings, resolved.limits, resolved.label
    agent = Agent(model, output_type=output_type, retries=2, name=task)
    result = await agent.run(prompt, model_settings=settings, usage_limits=limits)
    usage = result.usage
    registry.record_usage(
        task,
        label,
        input_tokens=usage.input_tokens or 0,
        output_tokens=usage.output_tokens or 0,
        cached=usage.cache_read_tokens or 0,
    )
    return result.output
