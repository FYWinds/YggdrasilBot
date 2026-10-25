"""How QQ users are shown to models: always ``名字(QQ号)``; the bot itself is ``{bot_name}(你)``.

Group cards and nicknames change at any time, so the QQ number is the identity and the name only
helps readability. Stored ``plain_text`` keeps raw ``@<qq>`` mentions; they are rewritten here at
render time with names resolved in one batched lookup per render.
"""

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, tuple_
from sqlalchemy.dialects.postgresql import distinct_on

from yggdrasil.core.log import LOGGER
from yggdrasil.db.models import Message, UserAlias
from yggdrasil.db.session import get_session

UNKNOWN_NAME = "群友"
_STRIP = str.maketrans("", "", '()（）<>"\r\n')
# ``@<qq>`` as written by normalize() for at segments; the lookbehind skips e-mail addresses.
MENTION = re.compile(r"(?<![A-Za-z0-9._%+-])@(\d{5,12})(?![\d(（])")
_SELF_LOOKBACK = timedelta(days=30)


def clean_name(name: str | None) -> str:
    """Name with characters that could fake a label or close a tag/attribute removed."""
    return " ".join((name or "").translate(_STRIP).split())


def label(name: str | None, user_id: int) -> str:
    """``名字(QQ)``; ``群友(QQ)`` when no name is known."""
    return f"{clean_name(name) or UNKNOWN_NAME}({user_id})"


def self_label(bot_name: str) -> str:
    return f"{clean_name(bot_name) or 'bot'}(你)"


def mentioned_ids(texts: Iterable[str]) -> set[int]:
    """QQ numbers mentioned as ``@<qq>`` in ``texts`` (to batch their name lookup)."""
    return {int(m) for text in texts for m in MENTION.findall(text)}


@dataclass(slots=True)
class People:
    """Resolved names for one render; build with :func:`resolve`."""

    bot_name: str
    names: dict[int, str] = field(default_factory=dict)
    self_ids: frozenset[int] = frozenset()

    def who(self, user_id: int, fallback: str | None = None, *, is_self: bool = False) -> str:
        """Label of a user: latest known alias, else ``fallback`` (e.g. the message's sender name)."""
        if is_self or user_id in self.self_ids:
            return self_label(self.bot_name)
        return label(self.names.get(user_id) or fallback, user_id)

    def mention(self, user_id: int) -> str:
        """Mention target without the ``@``; pass to ``normalize(..., mention=...)``."""
        return self.who(user_id)

    def mentions(self, text: str) -> str:
        """Rewrite ``@<qq>`` to ``@名字(QQ)`` (``@{bot_name}(你)`` for the bot); ``@name`` stays as is."""
        return MENTION.sub(lambda m: "@" + self.who(int(m.group(1))), text)


async def resolve(
    group_id: int | None,
    user_ids: Iterable[int] = (),
    *,
    bot_name: str,
    self_id: int | None = None,
    texts: Iterable[str] = (),
) -> People:
    """Latest alias of every user in ``user_ids`` and every ``@<qq>`` in ``texts``, in one query.

    ``self_id`` None: the bot's id is taken from its most recent own message in the group.
    """
    ids = {int(u) for u in user_ids if u} | mentioned_ids(texts)
    self_ids = {self_id} if self_id else set()
    if group_id is None or (not ids and self_ids):
        return People(bot_name, {}, frozenset(self_ids))
    names: dict[int, str] = {}
    async with get_session() as session:
        if ids:
            rows = await session.execute(
                select(UserAlias.user_id, UserAlias.name)
                .where(UserAlias.group_id == group_id, UserAlias.user_id.in_(ids))
                .ext(distinct_on(UserAlias.user_id))
                .order_by(UserAlias.user_id, UserAlias.last_seen.desc())
            )
            names = {u: n for u, n in rows.all() if n}
        if not self_ids:
            found = await session.scalar(
                select(Message.user_id)
                .where(
                    Message.group_id == group_id,
                    Message.is_self,
                    Message.sent_at > datetime.now(UTC) - _SELF_LOOKBACK,
                )
                .order_by(Message.sent_at.desc())
                .limit(1)
            )
            if found:
                self_ids.add(found)
    return People(bot_name, names, frozenset(self_ids))


async def names_in_groups(pairs: Iterable[tuple[int | None, int | None]]) -> dict[tuple[int, int], str]:
    """Latest alias per ``(group_id, user_id)`` for rows spanning several groups, in one query."""
    keys = {(g, u) for g, u in pairs if g and u}
    if not keys:
        return {}
    async with get_session() as session:
        rows = await session.execute(
            select(UserAlias.group_id, UserAlias.user_id, UserAlias.name)
            .where(tuple_(UserAlias.group_id, UserAlias.user_id).in_(keys))
            .ext(distinct_on(UserAlias.group_id, UserAlias.user_id))
            .order_by(UserAlias.group_id, UserAlias.user_id, UserAlias.last_seen.desc())
        )
        return {(g, u): n for g, u, n in rows.all() if n}


async def member_label(group_id: int | None, user_id: int | None) -> str:
    """For human-facing notices: ``名字(QQ)`` when the alias table knows the member, else the bare QQ."""
    if not group_id or not user_id:
        return str(user_id)
    try:
        name = (await names_in_groups([(group_id, user_id)])).get((group_id, user_id))
    except Exception:
        LOGGER.exception("alias lookup failed")
        name = None
    return label(name, user_id) if name else str(user_id)
