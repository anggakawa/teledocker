"""Tests for DockerClient.remove_container 404 tolerance.

When a Docker container has already been removed (crashed, manually deleted,
daemon restart), aiodocker raises a DockerError with status 404.
remove_container() must swallow this error and return normally so that
upstream callers can proceed with database cleanup.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from container_manager.docker_client import DockerClient, _is_not_found

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_docker_error(status: int, message: str = "not found") -> Exception:
    """Build an exception that mimics aiodocker.DockerError.

    aiodocker.DockerError stores the HTTP status code in a .status attribute.
    We simulate it with a plain Exception + manually set .status to avoid
    importing aiodocker internals in tests.
    """
    exc = Exception(message)
    exc.status = status
    return exc


def _make_connected_client() -> tuple[DockerClient, MagicMock]:
    """Return a DockerClient with a mock Docker connection injected."""
    client = DockerClient()
    mock_docker = MagicMock()
    client._docker = mock_docker
    return client, mock_docker


def _make_mock_container(
    delete_side_effect: Exception | None = None,
) -> MagicMock:
    """Build a mock container with configurable delete() behavior."""
    container = MagicMock()
    container.stop = AsyncMock()
    if delete_side_effect:
        container.delete = AsyncMock(side_effect=delete_side_effect)
    else:
        container.delete = AsyncMock()
    return container


# ---------------------------------------------------------------------------
# Tests: _is_not_found helper
# ---------------------------------------------------------------------------


class TestIsNotFound:
    """Verify the _is_not_found helper correctly identifies 404 exceptions."""

    def test_returns_true_for_404_status(self):
        """An exception with .status == 404 should be detected as not-found."""
        exc = _make_docker_error(404)

        assert _is_not_found(exc) is True

    def test_returns_false_for_500_status(self):
        """Server errors should not be treated as not-found."""
        exc = _make_docker_error(500, "internal server error")

        assert _is_not_found(exc) is False

    def test_returns_false_for_409_conflict(self):
        """Conflict errors (e.g. container in use) should propagate."""
        exc = _make_docker_error(409, "conflict")

        assert _is_not_found(exc) is False

    def test_returns_false_when_no_status_attribute(self):
        """A plain Exception without .status should not match."""
        exc = Exception("something went wrong")

        assert _is_not_found(exc) is False


# ---------------------------------------------------------------------------
# Tests: remove_container 404 handling
# ---------------------------------------------------------------------------


class TestRemoveContainerNotFoundHandling:
    """Verify remove_container() tolerates a missing Docker container."""

    @pytest.mark.asyncio
    async def test_completes_normally_when_delete_raises_404(self):
        """If container.delete() raises a 404, remove_container returns OK."""
        client, mock_docker = _make_connected_client()
        container = _make_mock_container(
            delete_side_effect=_make_docker_error(404),
        )
        mock_docker.containers.container.return_value = container

        # Should not raise.
        await client.remove_container("dead-container-id")

        container.delete.assert_called_once()

    @pytest.mark.asyncio
    async def test_propagates_non_404_errors(self):
        """Errors other than 404 (e.g. 500) must still propagate."""
        client, mock_docker = _make_connected_client()
        container = _make_mock_container(
            delete_side_effect=_make_docker_error(500, "internal error"),
        )
        mock_docker.containers.container.return_value = container

        with pytest.raises(Exception, match="internal error"):
            await client.remove_container("some-container-id")

    @pytest.mark.asyncio
    async def test_propagates_exceptions_without_status(self):
        """Generic exceptions without .status must still propagate."""
        client, mock_docker = _make_connected_client()
        container = _make_mock_container(
            delete_side_effect=RuntimeError("connection lost"),
        )
        mock_docker.containers.container.return_value = container

        with pytest.raises(RuntimeError, match="connection lost"):
            await client.remove_container("some-container-id")

    @pytest.mark.asyncio
    async def test_delete_called_with_correct_args(self):
        """Verify delete() is called with force=True and volume flag."""
        client, mock_docker = _make_connected_client()
        container = _make_mock_container()
        mock_docker.containers.container.return_value = container

        await client.remove_container("ctr-123", with_volume=True)

        container.delete.assert_called_once_with(v=True, force=True)

    @pytest.mark.asyncio
    async def test_stop_failure_does_not_prevent_delete(self):
        """Even if stop() fails, delete() must still be attempted."""
        client, mock_docker = _make_connected_client()
        container = _make_mock_container()
        container.stop = AsyncMock(side_effect=Exception("already stopped"))
        mock_docker.containers.container.return_value = container

        await client.remove_container("ctr-456")

        container.stop.assert_called_once()
        container.delete.assert_called_once()

    @pytest.mark.asyncio
    async def test_logs_warning_on_404(self):
        """A warning should be logged when the container is already gone."""
        client, mock_docker = _make_connected_client()
        container = _make_mock_container(
            delete_side_effect=_make_docker_error(404),
        )
        mock_docker.containers.container.return_value = container

        with patch("container_manager.docker_client.logger") as mock_logger:
            await client.remove_container("gone-container")

            mock_logger.warning.assert_called_once()
            assert "already removed" in mock_logger.warning.call_args[0][0]
