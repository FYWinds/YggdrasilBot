"""What a failed model run is recorded and shown as."""

from dataclasses import dataclass
from typing import Any

from pydantic_ai.exceptions import ContentFilterError, UnexpectedModelBehavior
from pydantic_ai.messages import ModelMessagesTypeAdapter, ModelResponse

# What a provider says about an answer it cut off (Anthropic's stop_details, Google's block reason).
_DETAIL_KEYS = ("refusal_category", "refusal", "block_reason")
_SUB_CHARS = 400


@dataclass(frozen=True, slots=True)
class Refusal:
    """The provider's content filter stopped the answer."""

    category: str | None  # the provider's label, e.g. Anthropic's "cyber"
    explanation: str | None  # the provider's own sentence about it


def _response_details(exc: UnexpectedModelBehavior) -> dict[str, Any] | None:
    """``provider_details`` of the response pydantic-ai attached as the error's body. That body is a dump of the whole
    response, thinking signatures first and the reason last, so cut to any length it shows everything but the reason.
    None when the body is something else."""
    try:
        messages = ModelMessagesTypeAdapter.validate_json(exc.body or "")
    except ValueError:
        return None
    last = messages[-1] if messages else None
    return (last.provider_details or {}) if isinstance(last, ModelResponse) else None


def refusal(exc: BaseException) -> Refusal | None:
    if not isinstance(exc, ContentFilterError):
        return None
    details = _response_details(exc) or {}
    category, explanation = details.get("refusal_category"), details.get("refusal")
    return Refusal(str(category) if category else None, str(explanation) if explanation else None)


def describe(exc: BaseException) -> str:
    """An exception as the text an invocation records: the reason first, so a caller's length cut keeps it. A group
    (every model of a fallback route failed) lists what each one failed with; its own message only counts them."""
    name = type(exc).__name__
    if isinstance(exc, BaseExceptionGroup):
        subs = "".join(f"\n- {describe(sub)[:_SUB_CHARS]}" for sub in exc.exceptions)
        return f"{name}: {exc.message}{subs}"
    if isinstance(exc, UnexpectedModelBehavior) and (details := _response_details(exc)) is not None:
        lines = "".join(f"\n{key}: {details[key]}" for key in _DETAIL_KEYS if details.get(key))
        return f"{name}: {exc.message}{lines}"
    return f"{name}: {exc}"
