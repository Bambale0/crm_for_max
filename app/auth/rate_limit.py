"""Atomic expiring login budgets and launch-data replay protection."""

import logging
from collections.abc import Awaitable
from typing import cast

from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.auth.security import token_digest

logger = logging.getLogger(__name__)

_LOGIN_BUDGET = """
local count = redis.call('INCR', KEYS[1])
local ttl = redis.call('TTL', KEYS[1])
if ttl < 0 then
    redis.call('EXPIRE', KEYS[1], ARGV[2])
    ttl = tonumber(ARGV[2])
end
if count > tonumber(ARGV[1]) then
    return math.max(ttl, 1)
end
return 0
"""


class LoginRateLimited(Exception):
    def __init__(self, retry_after: int) -> None:
        self.retry_after = retry_after


class RateLimiterUnavailable(Exception):
    """No login should proceed without shared rate and replay protection."""


class InitDataReplayed(Exception):
    """The signed launch data has already been consumed."""


async def check_login_budget(
    redis: Redis, *, scope: str, identifier: str, limit: int, window_seconds: int
) -> None:
    key = "auth:login:" + scope + ":" + token_digest(identifier)
    try:
        retry_after = await cast(
            Awaitable[int], redis.eval(_LOGIN_BUDGET, 1, key, str(limit), str(window_seconds))
        )
    except RedisError:
        logger.warning("login_rate_limiter_unavailable")
        raise RateLimiterUnavailable from None
    if retry_after:
        raise LoginRateLimited(retry_after)


async def consume_init_data(redis: Redis, *, signature: str, ttl_seconds: int) -> None:
    try:
        accepted = await redis.set(
            "auth:launch:" + token_digest(signature), "used", nx=True, ex=ttl_seconds
        )
    except RedisError:
        logger.warning("login_replay_protection_unavailable")
        raise RateLimiterUnavailable from None
    if not accepted:
        raise InitDataReplayed
