"""FastAPI application factory for the container manager service."""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from container_manager.cleanup import IdleContainerCleaner
from container_manager.config import settings
from container_manager.docker_client import DockerClient
from container_manager.health import ContainerHealthMonitor
from container_manager.routers import router

logging.basicConfig(level=settings.log_level)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialize Docker client and start background tasks."""
    logger.info("Starting container manager")

    docker_client = DockerClient()
    await docker_client.connect()
    app.state.docker_client = docker_client

    health_monitor = ContainerHealthMonitor(
        api_server_url=settings.api_server_url,
        service_token=settings.service_token,
    )
    health_monitor.start()

    idle_cleaner = IdleContainerCleaner(
        docker_client=docker_client,
        api_server_url=settings.api_server_url,
        service_token=settings.service_token,
        idle_timeout_minutes=settings.idle_timeout_minutes,
        destroy_timeout_hours=settings.destroy_timeout_hours,
    )
    idle_cleaner.start()

    yield

    logger.info("Shutting down container manager")
    await health_monitor.stop()
    await idle_cleaner.stop()
    await docker_client.disconnect()


def create_app() -> FastAPI:
    app = FastAPI(
        title="ChatOps Container Manager",
        description="Docker container lifecycle management for ChatOps AI Bridge",
        version="1.0.0",
        lifespan=lifespan,
    )
    app.include_router(router)
    return app


app = create_app()
