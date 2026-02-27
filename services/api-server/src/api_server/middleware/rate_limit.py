"""Sliding-window rate limiter backed by Redis sorted sets.

How it works:
- Each (user, action) pair has a Redis sorted set keyed by ratelimit:{id}:{action}.
- Set members are timestamps; scores are also timestamps so we can range-query.
- On each call: remove timestamps older than the window, count what remains,
  reject if over limit, otherwise add current timestamp and allow the request.
"""

import time

from fastapi import HTTPException, status
from redis.asyncio import Redis

# Rate limit configuration per action type.
_LIMITS: dict[str, tuple[int, int]] = {
    # action_type: (max_requests, window_seconds)
    "message": (20, 60),
    "container_op": (5, 60),
}

_DEFAULT_LIMIT = (30, 60)


async def check_rate_limit(
    telegram_id: int,
    action_type: str,
    redis: Redis,
) -> None:
    """Raise HTTP 429 if the user has exceeded the rate limit for this action.

    Args:
        telegram_id: The Telegram user ID being rate-limited.
        action_type: One of 'message', 'container_op', etc.
        redis: The shared async Redis client.
    """
    max_requests, window_seconds = _LIMITS.get(action_type, _DEFAULT_LIMIT)
    key = f"ratelimit:{telegram_id}:{action_type}"
    now = time.time()
    window_start = now - window_seconds

    # Atomic pipeline: remove old entries, count, add new entry, set expiry.
    async with redis.pipeline(transaction=True) as pipe:
        pipe.zremrangebyscore(key, "-inf", window_start)
        pipe.zcard(key)
        pipe.zadd(key, {str(now): now})
        pipe.expire(key, window_seconds)
        results = await pipe.execute()

    current_count = results[1]  # Result of ZCARD, before adding the new entry.
    if current_count >= max_requests:
        retry_after = int(window_seconds - (now - window_start))
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Rate limit exceeded. Try again in {retry_after} seconds.",
            headers={"Retry-After": str(retry_after)},
        )
