"""X (Twitter) posts, read through :mod:`yggdrasil.tools.x`."""

import httpx

from yggdrasil.core.log import LOGGER
from yggdrasil.tools import x
from yggdrasil.tools.errors import ToolError
from yggdrasil.tools.web.adapters.registry import site_adapter
from yggdrasil.tools.web.model import AdapterResult, Extracted


@site_adapter(*x.HOSTS)
async def post(client: httpx.AsyncClient, url: httpx.URL) -> AdapterResult:
    pid = x.post_id(str(url))
    if pid is None:
        return None
    from yggdrasil.runtime import snapshot

    try:
        post = await x.read_post(pid, x.provider(snapshot().search_providers), client=client)
    except x.PostGone:
        raise
    except ToolError as exc:
        LOGGER.warning("x post {} unreadable: {}", pid, exc)
        return None  # FxTwitter down and no X API: the generic path may still get the text
    return Extracted(
        final_url=post.url,
        title=f"{x.who(post)} 在 X 上的帖子",
        text=x.format_post(post),
        via=post.via,
        images=[m.url for m in post.images()],
    )
