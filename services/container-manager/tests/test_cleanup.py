"""Tests for the IdleContainerCleaner pause and auto-destroy logic.

The cleaner runs every 5 minutes and:
  1. Pauses running containers idle beyond idle_timeout_minutes.
  2. Destroys paused/stopped/error containers idle beyond destroy_timeout_hours.

We mock the httpx calls to the api-server and the DockerClient to test
the filtering and branching logic without real HTTP or Docker.
"""

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import httpx
import pytest

from container_manager.cleanup import IdleContainerCleaner


def _make_session(
    status: str = "running",
    idle_minutes: int = 0,
    container_id: str | None = None,
) -> dict:
    """Build a fake session dict matching the api-server JSON response."""
    session_id = str(uuid4())
    last_activity = datetime.now(timezone.utc) - timedelta(minutes=idle_minutes)
    return {
        "id": session_id,
        "status": status,
        "container_id": container_id or f"ctr-{session_id[:8]}",
        "last_activity_at": last_activity.isoformat(),
    }


def _build_cleaner(
    idle_timeout_minutes: int = 30,
    destroy_timeout_hours: int = 24,
) -> IdleContainerCleaner:
    """Build a cleaner with mocked dependencies."""
    docker_client = AsyncMock()
    return IdleContainerCleaner(
        docker_client=docker_client,
        api_server_url="http://api-server:8000",
        service_token="test-token",
        idle_timeout_minutes=idle_timeout_minutes,
        destroy_timeout_hours=destroy_timeout_hours,
    )


# ---------------------------------------------------------------------------
# Tests: pause logic (existing behavior, must stay intact)
# ---------------------------------------------------------------------------


class TestPauseIdleContainers:
    """Verify that running containers idle beyond the timeout get paused."""

    @pytest.mark.asyncio
    async def test_pauses_container_idle_beyond_timeout(self):
        """A running container idle for 31 min (timeout=30) should be paused."""
        cleaner = _build_cleaner(idle_timeout_minutes=30)
        session = _make_session(status="running", idle_minutes=31)

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = [session]

        mock_patch_response = MagicMock()

        with patch("container_manager.cleanup.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client.get = AsyncMock(return_value=mock_response)
            mock_client.patch = AsyncMock(return_value=mock_patch_response)
            mock_client_cls.return_value = mock_client

            await cleaner._pause_idle_containers()

        cleaner._docker.pause_container.assert_awaited_once_with(session["container_id"])

    @pytest.mark.asyncio
    async def test_skips_container_below_timeout(self):
        """A running container idle for only 10 min (timeout=30) should NOT be paused."""
        cleaner = _build_cleaner(idle_timeout_minutes=30)
        session = _make_session(status="running", idle_minutes=10)

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = [session]

        with patch("container_manager.cleanup.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client.get = AsyncMock(return_value=mock_response)
            mock_client_cls.return_value = mock_client

            await cleaner._pause_idle_containers()

        cleaner._docker.pause_container.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_handles_api_error_gracefully(self):
        """A failed API call should not crash the cleaner."""
        cleaner = _build_cleaner()

        with patch("container_manager.cleanup.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client.get = AsyncMock(side_effect=httpx.ConnectError("refused"))
            mock_client_cls.return_value = mock_client

            # Should not raise.
            await cleaner._pause_idle_containers()


# ---------------------------------------------------------------------------
# Tests: auto-destroy logic
# ---------------------------------------------------------------------------


class TestDestroyStaleContainers:
    """Verify that stale paused/stopped/error containers get destroyed."""

    @pytest.mark.asyncio
    async def test_destroys_paused_session_beyond_timeout(self):
        """A paused session idle for 25 hours (timeout=24h) should be destroyed."""
        cleaner = _build_cleaner(destroy_timeout_hours=24)
        session = _make_session(status="paused", idle_minutes=25 * 60)

        mock_get_response = MagicMock()
        mock_get_response.status_code = 200
        mock_get_response.json.return_value = [session]

        mock_delete_response = MagicMock()
        mock_delete_response.status_code = 200
        mock_delete_response.raise_for_status = MagicMock()

        with patch("container_manager.cleanup.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client.get = AsyncMock(return_value=mock_get_response)
            mock_client.delete = AsyncMock(return_value=mock_delete_response)
            mock_client_cls.return_value = mock_client

            await cleaner._destroy_stale_containers()

            mock_client.delete.assert_awaited_once()
            call_args = mock_client.delete.call_args
            assert session["id"] in call_args[0][0]

    @pytest.mark.asyncio
    async def test_destroys_stopped_session_beyond_timeout(self):
        """A stopped session idle for 25 hours should be destroyed."""
        cleaner = _build_cleaner(destroy_timeout_hours=24)
        session = _make_session(status="stopped", idle_minutes=25 * 60)

        mock_get_response = MagicMock()
        mock_get_response.status_code = 200
        mock_get_response.json.return_value = [session]

        mock_delete_response = MagicMock()
        mock_delete_response.status_code = 200
        mock_delete_response.raise_for_status = MagicMock()

        with patch("container_manager.cleanup.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client.get = AsyncMock(return_value=mock_get_response)
            mock_client.delete = AsyncMock(return_value=mock_delete_response)
            mock_client_cls.return_value = mock_client

            await cleaner._destroy_stale_containers()

            mock_client.delete.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_destroys_error_session_beyond_timeout(self):
        """An error session idle for 25 hours should be destroyed."""
        cleaner = _build_cleaner(destroy_timeout_hours=24)
        session = _make_session(status="error", idle_minutes=25 * 60)

        mock_get_response = MagicMock()
        mock_get_response.status_code = 200
        mock_get_response.json.return_value = [session]

        mock_delete_response = MagicMock()
        mock_delete_response.status_code = 200
        mock_delete_response.raise_for_status = MagicMock()

        with patch("container_manager.cleanup.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client.get = AsyncMock(return_value=mock_get_response)
            mock_client.delete = AsyncMock(return_value=mock_delete_response)
            mock_client_cls.return_value = mock_client

            await cleaner._destroy_stale_containers()

            mock_client.delete.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_skips_paused_session_below_timeout(self):
        """A paused session idle for only 2 hours (timeout=24h) should NOT be destroyed."""
        cleaner = _build_cleaner(destroy_timeout_hours=24)
        session = _make_session(status="paused", idle_minutes=2 * 60)

        mock_get_response = MagicMock()
        mock_get_response.status_code = 200
        mock_get_response.json.return_value = [session]

        with patch("container_manager.cleanup.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client.get = AsyncMock(return_value=mock_get_response)
            mock_client.delete = AsyncMock()
            mock_client_cls.return_value = mock_client

            await cleaner._destroy_stale_containers()

            mock_client.delete.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_skips_running_sessions(self):
        """Running sessions should never be destroyed, even if idle for days."""
        cleaner = _build_cleaner(destroy_timeout_hours=24)
        session = _make_session(status="running", idle_minutes=72 * 60)

        mock_get_response = MagicMock()
        mock_get_response.status_code = 200
        mock_get_response.json.return_value = [session]

        with patch("container_manager.cleanup.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client.get = AsyncMock(return_value=mock_get_response)
            mock_client.delete = AsyncMock()
            mock_client_cls.return_value = mock_client

            await cleaner._destroy_stale_containers()

            mock_client.delete.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_skips_creating_sessions(self):
        """Creating sessions should never be destroyed (in-flight provisioning)."""
        cleaner = _build_cleaner(destroy_timeout_hours=24)
        session = _make_session(status="creating", idle_minutes=72 * 60)

        mock_get_response = MagicMock()
        mock_get_response.status_code = 200
        mock_get_response.json.return_value = [session]

        with patch("container_manager.cleanup.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client.get = AsyncMock(return_value=mock_get_response)
            mock_client.delete = AsyncMock()
            mock_client_cls.return_value = mock_client

            await cleaner._destroy_stale_containers()

            mock_client.delete.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_handles_api_error_on_fetch(self):
        """A failed session fetch should not crash the cleaner."""
        cleaner = _build_cleaner()

        with patch("container_manager.cleanup.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client.get = AsyncMock(side_effect=httpx.ConnectError("refused"))
            mock_client_cls.return_value = mock_client

            await cleaner._destroy_stale_containers()

    @pytest.mark.asyncio
    async def test_handles_404_on_destroy(self):
        """A 404 on destroy (already gone) should be handled gracefully."""
        cleaner = _build_cleaner(destroy_timeout_hours=1)
        session = _make_session(status="paused", idle_minutes=120)

        mock_get_response = MagicMock()
        mock_get_response.status_code = 200
        mock_get_response.json.return_value = [session]

        mock_delete_response = MagicMock()
        mock_delete_response.status_code = 404

        with patch("container_manager.cleanup.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client.get = AsyncMock(return_value=mock_get_response)
            mock_client.delete = AsyncMock(return_value=mock_delete_response)
            mock_client_cls.return_value = mock_client

            # Should not raise.
            await cleaner._destroy_stale_containers()


# ---------------------------------------------------------------------------
# Tests: cleanup loop calls both steps
# ---------------------------------------------------------------------------


class TestCleanupLoop:
    """Verify that the cleanup loop invokes both pause and destroy."""

    @pytest.mark.asyncio
    async def test_loop_calls_both_pause_and_destroy(self):
        """A single loop iteration should call both pause and destroy methods."""
        cleaner = _build_cleaner()
        cleaner._pause_idle_containers = AsyncMock()
        cleaner._destroy_stale_containers = AsyncMock()

        # Run the loop body once by cancelling after the first sleep.
        original_sleep = asyncio.sleep

        call_count = 0

        async def fake_sleep(seconds):
            nonlocal call_count
            call_count += 1
            if call_count > 1:
                raise asyncio.CancelledError()

        with patch("container_manager.cleanup.asyncio.sleep", side_effect=fake_sleep):
            with pytest.raises(asyncio.CancelledError):
                await cleaner._cleanup_loop()

        cleaner._pause_idle_containers.assert_awaited_once()
        cleaner._destroy_stale_containers.assert_awaited_once()
