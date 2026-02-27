"""FastAPI application factory for the ChatOps API server."""

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from alembic import command as alembic_command
from alembic.config import Config as AlembicConfig
from fastapi import FastAPI
from redis.asyncio import Redis

from api_server.config import settings
from api_server.db.engine import dispose_engine, initialize_engine
from api_server.dependencies import set_redis_client
from api_server.middleware.error_handler import global_exception_handler
from api_server.routers import health, sessions, users

logging.basicConfig(level=settings.log_level)
logger = logging.getLogger(__name__)


def _run_migrations() -> None:
    """Apply any pending Alembic migrations (upgrade to head).

    Uses the DATABASE_URL from settings so the same connection string
    is used for both migrations and the running application.
    """
    migrations_dir = Path(__file__).parent / "db" / "migrations"
    alembic_cfg = AlembicConfig()
    alembic_cfg.set_main_option("script_location", str(migrations_dir))
    alembic_cfg.set_main_option("sqlalchemy.url", settings.database_url)
    alembic_command.upgrade(alembic_cfg, "head")
    logger.info("Database migrations applied successfully")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialize and cleanly shut down shared resources."""
    logger.info("Starting API server — initializing database and Redis connections")

    # Run Alembic migrations before anything else touches the database.
    # This is idempotent — if already at head, it does nothing.
    # Runs in a thread because env.py calls asyncio.run() which conflicts
    # with uvicorn's already-running event loop.
    await asyncio.to_thread(_run_migrations)

    initialize_engine(settings.database_url)
    redis_client = Redis.from_url(settings.redis_url, decode_responses=True)
    set_redis_client(redis_client)

    yield

    logger.info("Shutting down API server")
    await redis_client.aclose()
    await dispose_engine()


def create_app() -> FastAPI:
    app = FastAPI(
        title="ChatOps API Server",
        description="Internal REST API for the ChatOps AI Bridge system",
        version="1.0.0",
        # Disable docs in production; enable for development by removing these.
        docs_url="/docs",
        redoc_url=None,
        lifespan=lifespan,
    )

    app.add_exception_handler(Exception, global_exception_handler)

    app.include_router(health.router)
    app.include_router(users.router)
    app.include_router(sessions.router)

    return app


app = create_app()
