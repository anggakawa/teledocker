"""Alembic migration environment configured for async SQLAlchemy.

Async engines cannot run migrations synchronously; we wrap the migration
function in asyncio.run() so Alembic's sync migration runner works correctly.
The database URL is read from the ALEMBIC_DATABASE_URL env var (or falls back
to the alembic.ini setting), making it easy to override in CI or production.
"""

import asyncio
import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy.ext.asyncio import create_async_engine

# Import models so their metadata is populated before autogenerate runs.
from api_server.db.models import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Use an env var override if provided (useful in Docker Compose).
database_url = os.getenv("DATABASE_URL") or config.get_main_option("sqlalchemy.url")
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Run migrations using a literal SQL output (no DB connection needed)."""
    context.configure(
        url=database_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    """Run migrations against a live async database connection."""
    engine = create_async_engine(database_url, echo=False)

    async with engine.connect() as connection:
        await connection.run_sync(_run_sync_migrations)

    await engine.dispose()


def _run_sync_migrations(sync_connection):
    context.configure(
        connection=sync_connection,
        target_metadata=target_metadata,
    )
    with context.begin_transaction():
        context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
