"""Agent tools; failures are returned as text so the model can adapt.

Everything in ``ALL_TOOLS`` is read-only. Action tools (``action_tools``) change things and are only
handed to a run when the code has already decided the speaker may do that action. The sandbox workspace
suite lives in ``agent/tools/sandbox.py`` (its job tools in ``agent/tools/jobs.py``). This module never imports
them, and the suite's gate ``suite_enabled`` sits here, next to the handler policy it reads, so they need nothing
from it.
"""

from collections.abc import Awaitable, Callable

from pydantic_ai import RunContext, Tool

from yggdrasil.agent.deps import Deps
from yggdrasil.agent.tools import attachments, history, images, web
from yggdrasil.agent.tools.base import ToolResult, chat_tool
from yggdrasil.settings import Settings
from yggdrasil.tools.errors import ToolError

ALL_TOOLS: dict[str, Callable[..., Awaitable[ToolResult]]] = {
    "read_url": web.read_url,
    "web_search": web.web_search,
    "read_x": web.read_x,
    "search_x": web.search_x,
    "read_file": attachments.read_file,
    "view_image": attachments.view_image,
    "expand_forward": attachments.expand_forward,
    "search_history": history.search_history,
    "get_messages_around": history.get_messages_around,
    "recent_chat": history.recent_chat,
    "lookup_glossary": history.lookup_glossary,
    "get_profile": history.get_profile,
    "render_mermaid": images.render_mermaid,
    "render_markdown": images.render_markdown,
    "render_html": images.render_html,
    "generate_image": images.generate_image,
}


# chat gets recent chat injected into the prompt; tldr summarizes the target itself, so it fetches chat
# context only on demand and gets no profiles (they slant the summary toward the asker). The sandbox
# suite (pseudo tool name SANDBOX, see agent/tools/sandbox.py) is chat only.
SANDBOX = "sandbox"
_HANDLER_EXCLUDED: dict[str, frozenset[str]] = {
    "chat": frozenset({"recent_chat"}),
    "tldr": frozenset({"get_profile", "generate_image", SANDBOX}),
}


def _sandbox_requested(enabled: list[str], handler: str) -> bool:
    """Whether this handler's settings ask for the sandbox suite (deployment gates are checked separately)."""
    return SANDBOX in enabled and SANDBOX not in _HANDLER_EXCLUDED.get(handler, frozenset())


def suite_enabled(
    settings: Settings, handler: str, group_id: int | None, *, global_settings: Settings, configured: bool
) -> bool:
    """Whether this run gets the sandbox suite: group chat runs only, when the group's ``tools.enabled``
    names ``sandbox``, the global kill switch is on and the deployment has a sandbox configured."""
    return (
        group_id is not None
        and _sandbox_requested(settings.tools.enabled, handler)
        and global_settings.tools.sandbox_enabled
        and configured
    )


def tool_names(enabled: list[str], handler: str) -> list[str]:
    excluded = _HANDLER_EXCLUDED.get(handler, frozenset())
    return [name for name in enabled if name in ALL_TOOLS and name not in excluded]


def tools_for(names: list[str]) -> list[Tool[Deps]]:
    return [Tool(ALL_TOOLS[name], takes_ctx=True) for name in names]


@chat_tool("recall_message")
async def recall_message(ctx: RunContext[Deps]) -> str:
    """撤回被回复的那条消息（你自己发的）。成功后选 mode=done，不要再发消息；失败就照实说明。"""
    target = ctx.deps.recall_target
    if target is None:
        raise ToolError("这次没有可以撤回的消息")
    await ctx.deps.api.delete_msg(message_id=target)
    ctx.deps.actions.append(f"recall {target}")
    return "已撤回"


def action_tools(deps: Deps) -> list[Tool[Deps]]:
    """Action tools this run's speaker is allowed to use (decided in code before the run)."""
    return [Tool(recall_message, takes_ctx=True)] if deps.recall_target is not None else []
