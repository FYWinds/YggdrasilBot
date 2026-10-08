"""Compiled, immutable trigger index built from ``command_triggers``.

Semantics:
- Candidate rules = rules scoped to this group (or ``private``) + global rules. If a
  group defines any rule for a handler, the global rules of that handler are ignored
  in that group (group overrides global per handler).
- Candidates are tried in ascending ``priority`` (then id); the first match wins.
- ``require_mention`` rules only apply when the bot was @'d / replied to / named.
"""

import re
from dataclasses import dataclass
from functools import cached_property

from yggdrasil.runtime import Snapshot, TriggerRule

MATCH_TYPES = ("keyword_prefix", "keyword_exact", "regex", "mention", "reply_to_bot", "name_mention")
TARGETS = ("quoted_or_self", "self", "quoted")
ROLES = ("member", "admin", "owner", "superuser")


@dataclass(frozen=True, slots=True)
class MatchInput:
    scope: str  # group | private
    group_id: int | None
    text: str  # plaintext with the leading @bot already stripped by the adapter
    to_me: bool  # adapter: @bot / reply-to-bot / private chat
    replied_to_bot: bool
    role: str  # member | admin | owner | superuser


@dataclass(frozen=True, slots=True)
class Resolved:
    handler: str
    rule_id: int
    args: str
    captures: tuple[str, ...]
    target: str
    cooldown_s: int
    named: bool  # the bot's name appeared in the text


@dataclass(frozen=True)
class _Compiled:
    rule: TriggerRule
    regexes: tuple[re.Pattern[str], ...]


def _role_ok(rule: TriggerRule, role: str) -> bool:
    if not rule.allowed_roles or role == "superuser":
        return True
    allowed = set(rule.allowed_roles)
    # owner implies admin
    return role in allowed or (role == "owner" and "admin" in allowed)


class TriggerIndex:
    def __init__(self, snap: Snapshot) -> None:
        self._snap = snap
        compiled: list[_Compiled] = []
        for rule in snap.triggers:
            regexes = ()
            if rule.match_type == "regex":
                regexes = tuple(re.compile(p, re.IGNORECASE | re.DOTALL) for p in rule.patterns)
            compiled.append(_Compiled(rule, regexes))
        self._rules = compiled

    @cached_property
    def _by_scope(self) -> dict[tuple[str, int | None], list[_Compiled]]:
        out: dict[tuple[str, int | None], list[_Compiled]] = {}
        for c in self._rules:
            key = (c.rule.scope, c.rule.group_id if c.rule.scope == "group" else None)
            out.setdefault(key, []).append(c)
        return out

    def _candidates(self, scope: str, group_id: int | None) -> list[_Compiled]:
        if scope == "private":
            local = self._by_scope.get(("private", None), [])
            overridden = {c.rule.handler for c in local}
        else:
            local = self._by_scope.get(("group", group_id), [])
            overridden = {c.rule.handler for c in local}
        global_ = [c for c in self._by_scope.get(("global", None), []) if c.rule.handler not in overridden]
        # Private chats only see global rules that are explicitly private-safe (admin/privacy handlers).
        if scope == "private":
            global_ = [c for c in global_ if c.rule.handler.startswith(("admin.", "privacy."))]
        return sorted(local + global_, key=lambda c: (c.rule.priority, c.rule.id))

    def names(self, group_id: int | None) -> list[str]:
        persona = self._snap.settings(group_id).persona
        return [n for n in [persona.name, *persona.aliases] if n]

    def match(self, inp: MatchInput) -> Resolved | None:
        text = inp.text.strip()
        lowered = text.lower()
        named = any(n.lower() in lowered for n in self.names(inp.group_id))
        mentioned = inp.to_me or named
        for c in self._candidates(inp.scope, inp.group_id):
            rule = c.rule
            if rule.require_mention and not mentioned:
                continue
            if not _role_ok(rule, inp.role):
                continue
            hit = self._match_rule(c, text, lowered, inp, named)
            if hit is None:
                continue
            args, captures = hit
            if rule.arg_template:
                try:
                    args = rule.arg_template.format(*captures, rest=args.strip())
                except (IndexError, KeyError):
                    pass
            return Resolved(rule.handler, rule.id, args.strip(), captures, rule.target, rule.cooldown_s, named)
        return None

    @staticmethod
    def _match_rule(
        c: _Compiled, text: str, lowered: str, inp: MatchInput, named: bool
    ) -> tuple[str, tuple[str, ...]] | None:
        rule = c.rule
        match rule.match_type:
            case "keyword_prefix":
                for p in rule.patterns:
                    if lowered.startswith(p.lower()):
                        return text[len(p) :], ()
            case "keyword_exact":
                for p in rule.patterns:
                    if lowered == p.lower():
                        return "", ()
            case "regex":
                for rx in c.regexes:
                    if m := rx.search(text):
                        rest = (text[: m.start()] + text[m.end() :]).strip()
                        return rest, tuple(g or "" for g in m.groups())
            case "mention":
                if inp.to_me:
                    return text, ()
            case "reply_to_bot":
                if inp.replied_to_bot:
                    return text, ()
            case "name_mention":
                if named:
                    return text, ()
        return None


_index: TriggerIndex | None = None


def current() -> TriggerIndex:
    from yggdrasil.runtime import snapshot

    global _index
    if _index is None or _index._snap is not snapshot():
        _index = TriggerIndex(snapshot())
    return _index
