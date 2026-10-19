"""Sandbox error types.

How they reach the model: harness ``FileSystem`` tools (``ws_read_file`` / ``ws_edit_file``) turn any
``WorkspaceError`` (other than ``WorkspaceUnavailableError``) into ``ToolFailed`` with the exception text,
which the model reads as a failed tool call and the run continues; any other exception ends the run. So every
failure the model should read and work around (limits, a workspace that cannot be created right now) is a
``SandboxToolError``: a ``WorkspaceError`` for the harness tools and a ``SandboxError`` for the bot's
own sandbox tools. ``WorkspaceUnavailableError`` is kept for an environment that is really gone mid-run,
which pydantic-ai defines as run-ending.
"""

from pydantic_ai.workspaces import WorkspaceError


class SandboxError(Exception):
    """User/model-facing sandbox failure; the message is Chinese and safe to show."""


class SandboxNotConfigured(SandboxError):
    """The sandbox is not configured (token unset) or disabled."""


class SandboxToolError(SandboxError, WorkspaceError):
    """A failure the model sees as a failed tool call: per-invocation limits, acquisition failures."""
