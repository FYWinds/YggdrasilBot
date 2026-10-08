from functools import cache

import redis.asyncio as aioredis

from yggdrasil.config import REDIS_CONFIG


@cache
def redis_client() -> aioredis.Redis:
    return aioredis.Redis.from_url(str(REDIS_CONFIG.url), decode_responses=True)
