"""FastAPI application factory for the ChatOps API server."""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from redis.asyncio import Redis

from api_server.config import settings
from api_server.db.engine import dispose_engine, initialize_engine
from api_server.dependencies import set_redis_client
from api_server.middleware.error_handler import global_exception_handler
from api_server.routers import health, sessions, users

logging.basicConfig(level=settings.log_level)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialize and cleanly shut down shared resources."""
    logger.info("Starting API server — initializing database and Redis connections")

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
