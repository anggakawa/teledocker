"""Background task that pauses idle containers to free CPU and memory.

Every 5 minutes, fetches all running sessions and pauses any container
whose last_activity_at is older than the configured idle timeout.

Using docker pause (cgroups freezer) instead of docker stop means the
container resumes instantly (< 1s) on the next user message.
"""

import asyncio
import logging
from datetime import datetime, timezone

import httpx

from container_manager.docker_client import DockerClient

logger = logging.getLogger(__name__)

_CLEANUP_INTERVAL_SECONDS = 5 * 60  # 5 minutes


class IdleContainerCleaner:
    """Background task that pauses containers idle beyond the timeout."""

    def __init__(
        self,
        docker_client: DockerClient,
        api_server_url: str,
        service_token: str,
        idle_timeout_minutes: int,
    ):
        self._docker = docker_client
        self._api_server_url = api_server_url
        self._service_token = service_token
        self._idle_timeout_minutes = idle_timeout_minutes
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        self._task = asyncio.create_task(self._cleanup_loop())
        logger.info(
            "Idle container cleaner started (timeout=%s min)", self._idle_timeout_minutes
        )

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _cleanup_loop(self) -> None:
        while True:
            await asyncio.sleep(_CLEANUP_INTERVAL_SECONDS)
            await self._pause_idle_containers()

    async def _pause_idle_containers(self) -> None:
        """Pause all running containers that have been idle too long."""
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.get(
                    f"{self._api_server_url}/api/v1/sessions",
                    params={"status": "running"},
                    headers={"X-Service-Token": self._service_token},
                )
                if response.status_code != 200:
                    return
                sessions = response.json()
        except Exception as exc:
            logger.warning("Idle cleaner failed to fetch sessions: %s", exc)
            return

        now = datetime.now(timezone.utc)
        idle_threshold_seconds = self._idle_timeout_minutes * 60

        for session in sessions:
            last_activity_str = session.get("last_activity_at")
            container_id = session.get("container_id")
            if not last_activity_str or not container_id:
                continue

            last_activity = datetime.fromisoformat(last_activity_str)
            idle_seconds = (now - last_activity).total_seconds()

            if idle_seconds > idle_threshold_seconds:
                await self._pause_session(session["id"], container_id)

    async def _pause_session(self, session_id: str, container_id: str) -> None:
        """Pause the container and update its status in the API server."""
        try:
            await self._docker.pause_container(container_id)
            logger.info("Paused idle container %s (session %s)", container_id, session_id)
        except Exception as exc:
            logger.warning("Failed to pause container %s: %s", container_id, exc)
            return

        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                await client.patch(
                    f"{self._api_server_url}/api/v1/sessions/{session_id}/status",
                    json={"status": "paused"},
                    headers={"X-Service-Token": self._service_token},
                )
        except Exception as exc:
            logger.warning("Failed to update session %s status to paused: %s", session_id, exc)
