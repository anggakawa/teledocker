"""Background task that periodically checks if agent bridges are alive.

Every 30 seconds, attempts a WebSocket ping to port 9100 of each running
container. If unreachable, marks the session as 'error' in the API server
so the user gets a notification and can restart.
"""

import asyncio
import json
import logging

import httpx
import websockets
from websockets.exceptions import WebSocketException

logger = logging.getLogger(__name__)

_HEALTH_CHECK_INTERVAL_SECONDS = 30
_WEBSOCKET_TIMEOUT_SECONDS = 5


class ContainerHealthMonitor:
    """Async background task for container health monitoring."""

    def __init__(self, api_server_url: str, service_token: str):
        self._api_server_url = api_server_url
        self._service_token = service_token
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        """Start the background monitoring loop."""
        self._task = asyncio.create_task(self._monitor_loop())
        logger.info("Container health monitor started")

    async def stop(self) -> None:
        """Cancel the monitoring loop gracefully."""
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _monitor_loop(self) -> None:
        while True:
            await asyncio.sleep(_HEALTH_CHECK_INTERVAL_SECONDS)
            await self._check_all_running_containers()

    async def _check_all_running_containers(self) -> None:
        """Fetch all running sessions from api-server and ping each bridge."""
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
            logger.warning("Health monitor failed to fetch sessions: %s", exc)
            return

        # Check each session concurrently but with bounded concurrency.
        semaphore = asyncio.Semaphore(10)
        tasks = [
            self._check_session(session, semaphore)
            for session in sessions
            if session.get("container_id")
        ]
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _check_session(self, session: dict, semaphore: asyncio.Semaphore) -> None:
        """Ping the agent bridge WebSocket. Mark session as error if unreachable."""
        async with semaphore:
            container_name = session.get("container_name", "")
            session_id = session["id"]

            try:
                uri = f"ws://{container_name}:9100"
                async with websockets.connect(uri, open_timeout=_WEBSOCKET_TIMEOUT_SECONDS) as ws:
                    ping_message = json.dumps(
                        {"method": "health_check", "params": {}, "id": "ping"}
                    )
                    await ws.send(ping_message)
                    await asyncio.wait_for(ws.recv(), timeout=_WEBSOCKET_TIMEOUT_SECONDS)
            except (WebSocketException, OSError, asyncio.TimeoutError) as exc:
                logger.warning(
                    "Agent bridge unreachable for session %s: %s", session_id, exc
                )
                await self._mark_session_error(session_id)

    async def _mark_session_error(self, session_id: str) -> None:
        """Update the session status to 'error' in the API server."""
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                await client.patch(
                    f"{self._api_server_url}/api/v1/sessions/{session_id}/status",
                    json={"status": "error"},
                    headers={"X-Service-Token": self._service_token},
                )
        except Exception as exc:
            logger.warning("Failed to mark session %s as error: %s", session_id, exc)
