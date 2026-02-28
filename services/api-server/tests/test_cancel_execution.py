"""Tests for the cancel_execution endpoint in sessions.py.

POST /{session_id}/cancel signals the agent to abort the current execution.
It proxies the cancel to the container-manager's /containers/{id}/cancel route.

Follows the same test pattern as test_new_conversation.py:
1. Session not found -> HTTP 404
2. Session has no container_id -> HTTP 400
3. Happy path -> POST proxied to container-manager with correct URL and token
"""

import os

# Set required env vars before any api_server module is imported.
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
os.environ.setdefault("ENCRYPTION_KEY_HEX", "a" * 64)
os.environ.setdefault("SERVICE_TOKEN", "test-token")
os.environ.setdefault("CONTAINER_MANAGER_URL", "http://container-manager:8001")

from unittest.mock import AsyncMock, MagicMock, patch  # noqa: E402
from uuid import uuid4  # noqa: E402

import pytest  # noqa: E402
from fastapi import HTTPException  # noqa: E402


def _make_mock_session(container_id: str | None) -> MagicMock:
    """Return a lightweight mock of a SessionDTO with a given container_id."""
    mock_session = MagicMock()
    mock_session.container_id = container_id
    return mock_session


def _make_mock_httpx_client(mock_response: AsyncMock) -> tuple[MagicMock, AsyncMock]:
    """Return (MockClient class, mock_client_instance) wired as an async context manager."""
    mock_client_instance = AsyncMock()
    mock_client_instance.post = AsyncMock(return_value=mock_response)

    mock_client_class = MagicMock()
    mock_client_class.return_value.__aenter__ = AsyncMock(return_value=mock_client_instance)
    mock_client_class.return_value.__aexit__ = AsyncMock(return_value=False)

    return mock_client_class, mock_client_instance


class TestCancelExecutionEndpoint:
    """Unit tests for the cancel_execution endpoint in sessions.py."""

    @pytest.mark.asyncio
    async def test_session_not_found_raises_404(self):
        """When session doesn't exist, raise HTTP 404."""
        mock_db = AsyncMock()
        session_id = uuid4()

        with patch("api_server.routers.sessions.session_service") as mock_service:
            mock_service.get_session = AsyncMock(return_value=None)

            from api_server.routers.sessions import cancel_execution

            with pytest.raises(HTTPException) as exc_info:
                await cancel_execution(session_id=session_id, db=mock_db)

        assert exc_info.value.status_code == 404
        assert "not found" in exc_info.value.detail.lower()

    @pytest.mark.asyncio
    async def test_no_container_raises_400(self):
        """When session exists but has no container_id, raise HTTP 400."""
        mock_db = AsyncMock()
        session_id = uuid4()
        session_without_container = _make_mock_session(container_id=None)

        with patch("api_server.routers.sessions.session_service") as mock_service:
            mock_service.get_session = AsyncMock(return_value=session_without_container)

            from api_server.routers.sessions import cancel_execution

            with pytest.raises(HTTPException) as exc_info:
                await cancel_execution(session_id=session_id, db=mock_db)

        assert exc_info.value.status_code == 400

    @pytest.mark.asyncio
    async def test_proxies_post_to_container_manager(self):
        """Happy path: POST is forwarded to container-manager cancel endpoint."""
        mock_db = AsyncMock()
        session_id = uuid4()
        container_id = "container-abc"
        session_with_container = _make_mock_session(container_id=container_id)

        mock_response = AsyncMock()
        mock_response.raise_for_status = MagicMock()

        mock_client_class, mock_client_instance = _make_mock_httpx_client(mock_response)

        with (
            patch("api_server.routers.sessions.session_service") as mock_service,
            patch("api_server.routers.sessions.httpx.AsyncClient", mock_client_class),
        ):
            mock_service.get_session = AsyncMock(return_value=session_with_container)

            from api_server.routers.sessions import cancel_execution

            await cancel_execution(session_id=session_id, db=mock_db)

        mock_client_instance.post.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_proxy_url_contains_container_id(self):
        """The forwarded POST URL must include the container_id and 'cancel'."""
        mock_db = AsyncMock()
        session_id = uuid4()
        container_id = "specific-container-xyz"
        session_with_container = _make_mock_session(container_id=container_id)

        mock_response = AsyncMock()
        mock_response.raise_for_status = MagicMock()

        mock_client_class, mock_client_instance = _make_mock_httpx_client(mock_response)

        with (
            patch("api_server.routers.sessions.session_service") as mock_service,
            patch("api_server.routers.sessions.httpx.AsyncClient", mock_client_class),
        ):
            mock_service.get_session = AsyncMock(return_value=session_with_container)

            from api_server.routers.sessions import cancel_execution

            await cancel_execution(session_id=session_id, db=mock_db)

        call_args = mock_client_instance.post.call_args
        forwarded_url = call_args.args[0]

        assert container_id in forwarded_url
        assert "cancel" in forwarded_url

    @pytest.mark.asyncio
    async def test_proxy_includes_service_token_header(self):
        """The forwarded POST must include the X-Service-Token header."""
        mock_db = AsyncMock()
        session_id = uuid4()
        container_id = "container-auth"
        session_with_container = _make_mock_session(container_id=container_id)

        mock_response = AsyncMock()
        mock_response.raise_for_status = MagicMock()

        mock_client_class, mock_client_instance = _make_mock_httpx_client(mock_response)

        with (
            patch("api_server.routers.sessions.session_service") as mock_service,
            patch("api_server.routers.sessions.httpx.AsyncClient", mock_client_class),
        ):
            mock_service.get_session = AsyncMock(return_value=session_with_container)

            from api_server.routers.sessions import cancel_execution

            await cancel_execution(session_id=session_id, db=mock_db)

        call_args = mock_client_instance.post.call_args
        forwarded_headers = call_args.kwargs.get("headers", {})
        assert "X-Service-Token" in forwarded_headers
        assert forwarded_headers["X-Service-Token"]

    @pytest.mark.asyncio
    async def test_raise_for_status_called(self):
        """raise_for_status() must be called on the proxy response."""
        mock_db = AsyncMock()
        session_id = uuid4()
        container_id = "container-status"
        session_with_container = _make_mock_session(container_id=container_id)

        mock_response = AsyncMock()
        mock_response.raise_for_status = MagicMock()

        mock_client_class, mock_client_instance = _make_mock_httpx_client(mock_response)

        with (
            patch("api_server.routers.sessions.session_service") as mock_service,
            patch("api_server.routers.sessions.httpx.AsyncClient", mock_client_class),
        ):
            mock_service.get_session = AsyncMock(return_value=session_with_container)

            from api_server.routers.sessions import cancel_execution

            await cancel_execution(session_id=session_id, db=mock_db)

        mock_response.raise_for_status.assert_called_once()
