"""FastAPI dependency providers for database sessions, Redis, and service auth."""

from collections.abc import AsyncGenerator

from fastapi import Depends, Header, HTTPException, status
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from api_server.config import settings
from api_server.db.engine import get_db

# Redis client is initialized once in the lifespan and stored here.
_redis_client: Redis | None = None


def set_redis_client(client: Redis) -> None:
    """Called during app startup to register the shared Redis client."""
    global _redis_client
    _redis_client = client


async def get_redis() -> Redis:
    """FastAPI dependency that returns the shared async Redis client."""
    if _redis_client is None:
        raise RuntimeError("Redis client not initialized")
    return _redis_client


async def verify_service_token(
    x_service_token: str = Header(..., alias="X-Service-Token"),
) -> None:
    """Reject requests with missing or invalid service token.

    This is used as a dependency on every endpoint so internal APIs cannot
    be called from outside the Docker network without the shared secret.
    """
    if x_service_token != settings.service_token:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid or missing service token",
        )
