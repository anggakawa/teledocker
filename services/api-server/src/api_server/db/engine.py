"""Async SQLAlchemy engine and session factory for the API server.

Uses asyncpg as the PostgreSQL driver (postgresql+asyncpg:// connection strings).
expire_on_commit=False prevents lazy-load errors after a commit closes the session.
"""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

# Module-level engine and session factory, initialized once at app startup.
_engine = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def initialize_engine(database_url: str) -> None:
    """Create the async engine and session factory from a connection URL.

    Called once during FastAPI lifespan startup so the URL comes from config,
    not from a module-level import that would run before settings are loaded.
    """
    global _engine, _session_factory

    _engine = create_async_engine(
        database_url,
        echo=False,  # Set to True for SQL query logging during development.
        pool_size=10,
        max_overflow=20,
        pool_pre_ping=True,  # Detect and replace dead connections before checkout.
    )
    _session_factory = async_sessionmaker(
        bind=_engine,
        expire_on_commit=False,
        class_=AsyncSession,
    )


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    if _session_factory is None:
        raise RuntimeError("Engine not initialized. Call initialize_engine() first.")
    return _session_factory


@asynccontextmanager
async def get_db_session() -> AsyncGenerator[AsyncSession, None]:
    """Async context manager yielding a database session with automatic rollback."""
    factory = get_session_factory()
    async with factory() as session:
        try:
            yield session
        except BaseException:
            await session.rollback()
            raise


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency that yields a database session per request."""
    async with get_db_session() as session:
        yield session


async def dispose_engine() -> None:
    """Close all connections in the pool. Called during app shutdown."""
    if _engine is not None:
        await _engine.dispose()
