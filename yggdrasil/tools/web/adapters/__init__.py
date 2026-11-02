"""Site adapters: official APIs and raw sources for sites whose pages read badly.

Importing a site module registers its adapter; :func:`adapter_for` finds the one for a URL."""

from yggdrasil.tools.web.adapters import (  # noqa: F401 - imported for their registrations
    bilibili,
    github,
    hackernews,
    huggingface,
    msn,
    statuspage,
    tangled,
    telegram,
    ubuntu,
    x_post,
    youtube,
)
from yggdrasil.tools.web.adapters.registry import adapter_for

__all__ = ["adapter_for"]
