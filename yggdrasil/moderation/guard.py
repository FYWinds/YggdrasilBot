"""Qwen3Guard-Gen scoring and policy.

The model answers ``Safety: Safe|Unsafe|Controversial`` + ``Categories: ...``. A
continuous score comes from the logprobs of the verdict token:
``score = P(Unsafe) + 0.5 * P(Controversial)``. Providers that don't return
logprobs fall back to Safe=0 / Controversial=0.5 / Unsafe=1.
"""

import math
import re
import time
from dataclasses import asdict, dataclass, field
from typing import Literal

from yggdrasil.core.log import LOGGER
from yggdrasil.core.metrics import MODERATION_DURATION, MODERATION_VERDICTS
from yggdrasil.core.telemetry import TRACER
from yggdrasil.llm.registry import TaskNotConfigured, openai_client, record_usage, route
from yggdrasil.runtime import snapshot
from yggdrasil.settings import ModerationSettings

Stage = Literal["input", "output"]
Action = Literal["pass", "review", "drop"]
_SEVERITY: dict[str, int] = {"pass": 0, "review": 1, "drop": 2}
# Leading characters of the verdict token; " Un" / " Cont" partial tokens count too.
_VERDICT_PREFIX = {"saf": "Safe", "un": "Unsafe", "con": "Controversial"}
_CATEGORIES = re.compile(r"Categories:\s*(.+)", re.IGNORECASE)
_SAFETY = re.compile(r"Safety:\s*(Safe|Unsafe|Controversial)", re.IGNORECASE)


@dataclass(slots=True)
class Verdict:
    stage: str
    verdict: str  # Safe | Unsafe | Controversial | Skipped | Error
    score: float
    categories: list[str] = field(default_factory=list)
    action: Action = "pass"
    model: str = ""
    latency_ms: int = 0

    def as_dict(self) -> dict:
        return asdict(self)


def _parse_categories(text: str) -> list[str]:
    m = _CATEGORIES.search(text)
    if not m:
        return []
    cats = [c.strip() for c in m.group(1).split("\n")[0].split(",")]
    return [c for c in cats if c and c.lower() != "none"]


def _score_from_logprobs(logprobs: list | None, verdict: str) -> float:
    """Distribution over the token right after ``Safety:``."""
    fallback = {"Safe": 0.0, "Controversial": 0.5, "Unsafe": 1.0}.get(verdict, 0.0)
    if not logprobs:
        return fallback
    seen = ""
    for tok in logprobs:
        if not seen.rstrip().lower().endswith("safety:"):
            seen += getattr(tok, "token", None) or ""
            continue
        probs = {"Safe": 0.0, "Unsafe": 0.0, "Controversial": 0.0}
        for alt in getattr(tok, "top_logprobs", None) or []:
            alt_tok = (alt.token or "").strip().lower()
            name = next((n for p, n in _VERDICT_PREFIX.items() if alt_tok.startswith(p)), None)
            if name is not None:
                probs[name] += math.exp(alt.logprob)
        total = sum(probs.values())
        if total <= 0:
            return fallback
        return round((probs["Unsafe"] + 0.5 * probs["Controversial"]) / total, 4)
    return fallback


def decide_action(verdict: str, score: float, categories: list[str], policy: ModerationSettings) -> Action:
    if verdict not in ("Unsafe", "Controversial"):
        return "pass"
    rules = [policy.category_rules.get(c, policy.default_rule) for c in categories] or [policy.default_rule]
    action: Action = "pass"
    for rule in rules:
        if score >= rule.threshold and _SEVERITY[rule.action] > _SEVERITY[action]:
            action = rule.action
    return action


async def moderate(stage: Stage, prompt: str, response: str | None = None, *, group_id: int | None = None) -> Verdict:
    snap = snapshot()
    policy = snap.settings(group_id).moderation
    enabled = policy.enabled and {"input": policy.check_input, "output": policy.check_output}[stage]
    if not enabled:
        return Verdict(stage, "Skipped", 0.0)
    try:
        r = route("moderation", snap)
    except TaskNotConfigured:
        return Verdict(stage, "Skipped", 0.0)
    provider = snap.providers.get(r.provider_id)
    if provider is None or not provider.enabled:
        return Verdict(stage, "Skipped", 0.0)

    messages = [{"role": "user", "content": prompt[:6000]}]
    if response is not None:
        messages.append({"role": "assistant", "content": response[:6000]})
    started = time.perf_counter()
    with TRACER.start_as_current_span(f"moderation {stage}", attributes={"moderation.stage": stage}) as span:
        try:
            resp = await openai_client(provider).chat.completions.create(
                model=r.model,
                messages=messages,  # pyright: ignore[reportArgumentType]
                max_tokens=48,
                temperature=0,
                logprobs=True,
                top_logprobs=5,
            )
        except Exception as e:
            LOGGER.warning("moderation call failed ({}): {}", stage, e)
            span.record_exception(e)
            # Fail open on input/ack, closed (review) on output so nothing unchecked is posted.
            action: Action = "review" if stage == "output" else "pass"
            return Verdict(stage, "Error", 0.0, action=action, model=r.model)
        elapsed = time.perf_counter() - started
        choice = resp.choices[0]
        text = choice.message.content or ""
        m = _SAFETY.search(text)
        verdict = m.group(1).capitalize() if m else "Safe"
        if verdict.lower() == "controversial":
            verdict = "Controversial"
        categories = _parse_categories(text) if verdict != "Safe" else []
        score = _score_from_logprobs(choice.logprobs.content if choice.logprobs else None, verdict)
        action = decide_action(verdict, score, categories, policy)
        span.set_attributes({"moderation.verdict": verdict, "moderation.score": score, "moderation.action": action})
        if resp.usage:
            record_usage(
                "moderation",
                r.model,
                input_tokens=resp.usage.prompt_tokens,
                output_tokens=resp.usage.completion_tokens,
            )
    MODERATION_DURATION.record(elapsed, {"stage": stage})
    for cat in categories or ["None"]:
        MODERATION_VERDICTS.add(1, {"stage": stage, "verdict": verdict, "category": cat, "action": action})
    return Verdict(stage, verdict, score, categories, action, r.model, int(elapsed * 1000))
