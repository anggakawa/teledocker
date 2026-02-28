"""Tests for the new_conversation endpoint.

POST /{session_id}/new-conversation resets the Claude conversation history
inside a running container without restarting it. It is a thin proxy:
it looks up the session, validates it has a container, then forwards a POST
to the container-manager's /containers/{container_id}/new-conversation route.

The three behaviours under test:

  1. Session not found -> HTTP 404.
  2. Session has no container_id -> HTTP 400 with the standard "no container" message.
  3. Happy path -> POST is forwarded to the container-manager with the correct
     URL and service-token header.

Because importing api_server.routers.sessions causes pydantic-settings to
instantiate ApiServerSettings() at module level — which requires DATABASE_URL,
ENCRYPTION_KEY_HEX, and SERVICE_TOKEN to be present in the environment — we
set those variables before any api_server import. This is the same technique
used in test_db_session_cancellation.py.

Reference: api_server/routers/sessions.py, lines 228-250.
"""

import os

# Set required env vars before any api_server module is imported.
# These values are throwaway test credentials; they never reach a real service.
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
os.environ.setdefault("ENCRYPTION_KEY_HEX", "a" * 64)
os.environ.setdefault("SERVICE_TOKEN", "test-token")
os.environ.setdefault("CONTAINER_MANAGER_URL", "http://container-manager:8001")

from unittest.mock import AsyncMock, MagicMock, patch  # noqa: E402
from uuid import uuid4  # noqa: E402

import pytest  # noqa: E402
from fastapi import HTTPException  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers: build reusable mock objects
# ---------------------------------------------------------------------------


def _make_mock_session(container_id: str | None) -> MagicMock:
    """Return a lightweight mock of a SessionDTO with a given container_id.

    All other fields are irrelevant to the endpoint under test, so we only
    set container_id. Using MagicMock lets attribute access succeed for any
    other field the endpoint might read in the future without causing an error.
    """
    mock_session = MagicMock()
    mock_session.container_id = container_id
    return mock_session


def _make_mock_httpx_client(mock_response: AsyncMock) -> tuple[MagicMock, AsyncMock]:
    """Return (MockClient class, mock_client_instance) wired as an async context manager.

    The endpoint uses `async with httpx.AsyncClient(...) as client:` so we need
    __aenter__ to return the instance and __aexit__ to return False (no suppression).
    """
    mock_client_instance = AsyncMock()
    mock_client_instance.post = AsyncMock(return_value=mock_response)

    mock_client_class = MagicMock()
    mock_client_class.return_value.__aenter__ = AsyncMock(return_value=mock_client_instance)
    mock_client_class.return_value.__aexit__ = AsyncMock(return_value=False)

    return mock_client_class, mock_client_instance


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestNewConversationEndpoint:
    """Unit tests for the new_conversation endpoint in sessions.py."""

    @pytest.mark.asyncio
    async def test_session_not_found_raises_404(self):
        """When session_service.get_session returns None, the endpoint raises HTTP 404.

        A missing session must never reach the container-manager — the endpoint
        must short-circuit immediately with a clear not-found response.
        """
        mock_db = AsyncMock()
        session_id = uuid4()

        with patch("api_server.routers.sessions.session_service") as mock_service:
            mock_service.get_session = AsyncMock(return_value=None)

            from api_server.routers.sessions import new_conversation

            with pytest.raises(HTTPException) as exc_info:
                await new_conversation(session_id=session_id, db=mock_db)

        raised_exception = exc_info.value
        assert raised_exception.status_code == 404, (
            f"Expected HTTP 404 for a missing session, got {raised_exception.status_code}."
        )
        assert "not found" in raised_exception.detail.lower(), (
            f"404 detail should mention 'not found'. Got: {raised_exception.detail!r}"
        )

    @pytest.mark.asyncio
    async def test_no_container_raises_400(self):
        """When the session exists but has no container_id, the endpoint raises HTTP 400.

        container_id is None when the container provisioning step failed.
        The endpoint must reject the request before trying to proxy anywhere.
        """
        mock_db = AsyncMock()
        session_id = uuid4()

        # Session exists but provisioning never assigned a container.
        session_without_container = _make_mock_session(container_id=None)

        with patch("api_server.routers.sessions.session_service") as mock_service:
            mock_service.get_session = AsyncMock(return_value=session_without_container)

            from api_server.routers.sessions import new_conversation

            with pytest.raises(HTTPException) as exc_info:
                await new_conversation(session_id=session_id, db=mock_db)

        raised_exception = exc_info.value
        assert raised_exception.status_code == 400, (
            f"Expected HTTP 400 for a session with no container, "
            f"got {raised_exception.status_code}."
        )

    @pytest.mark.asyncio
    async def test_no_container_detail_matches_constant(self):
        """The 400 detail must equal the shared _NO_CONTAINER_ERROR constant.

        Telegram-bot and other clients may match this exact string. A silent
        change here would break downstream error handling without a test failure.
        """
        # This is the canonical value from sessions.py line 31.
        # Any change to the production constant must also update this assertion.
        expected_detail = "Session has no container — it may have failed to provision."

        mock_db = AsyncMock()
        session_id = uuid4()
        session_without_container = _make_mock_session(container_id=None)

        with patch("api_server.routers.sessions.session_service") as mock_service:
            mock_service.get_session = AsyncMock(return_value=session_without_container)

            from api_server.routers.sessions import new_conversation

            with pytest.raises(HTTPException) as exc_info:
                await new_conversation(session_id=session_id, db=mock_db)

        actual_detail = exc_info.value.detail
        assert actual_detail == expected_detail, (
            f"400 detail must match _NO_CONTAINER_ERROR exactly.\n"
            f"Expected: {expected_detail!r}\n"
            f"Got:      {actual_detail!r}"
        )

    @pytest.mark.asyncio
    async def test_proxies_post_to_container_manager(self):
        """When the session has a container, a POST is forwarded to the container-manager.

        The endpoint must call httpx.AsyncClient.post() exactly once, targeting
        the container-manager URL built from settings and the container_id.
        """
        mock_db = AsyncMock()
        session_id = uuid4()
        container_id = "abc123"

        session_with_container = _make_mock_session(container_id=container_id)

        mock_response = AsyncMock()
        mock_response.raise_for_status = MagicMock()  # synchronous in httpx

        mock_client_class, mock_client_instance = _make_mock_httpx_client(mock_response)

        with (
            patch("api_server.routers.sessions.session_service") as mock_service,
            patch("api_server.routers.sessions.httpx.AsyncClient", mock_client_class),
        ):
            mock_service.get_session = AsyncMock(return_value=session_with_container)

            from api_server.routers.sessions import new_conversation

            # This must complete without raising — the response is None (204).
            await new_conversation(session_id=session_id, db=mock_db)

        mock_client_instance.post.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_proxy_url_contains_container_id(self):
        """The forwarded POST URL must embed the container_id from the session.

        A wrong URL (e.g. /containers/None/...) would silently target the wrong
        container or produce a cryptic 404 from the container-manager.
        """
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

            from api_server.routers.sessions import new_conversation

            await new_conversation(session_id=session_id, db=mock_db)

        # Inspect the URL that was passed to client.post().
        call_args = mock_client_instance.post.call_args
        forwarded_url = call_args.args[0]

        assert container_id in forwarded_url, (
            f"Forwarded URL must contain the container_id '{container_id}'.\n"
            f"Got URL: {forwarded_url!r}"
        )
        assert "new-conversation" in forwarded_url, (
            f"Forwarded URL must contain the 'new-conversation' path segment.\n"
            f"Got URL: {forwarded_url!r}"
        )

    @pytest.mark.asyncio
    async def test_proxy_request_includes_service_token_header(self):
        """The forwarded POST must include the X-Service-Token header.

        The container-manager rejects requests without a valid service token.
        Omitting the header would cause the proxy to always return 401 or 403
        from the container-manager, silently failing new-conversation resets.
        """
        mock_db = AsyncMock()
        session_id = uuid4()
        container_id = "container-for-auth-test"

        session_with_container = _make_mock_session(container_id=container_id)

        mock_response = AsyncMock()
        mock_response.raise_for_status = MagicMock()

        mock_client_class, mock_client_instance = _make_mock_httpx_client(mock_response)

        with (
            patch("api_server.routers.sessions.session_service") as mock_service,
            patch("api_server.routers.sessions.httpx.AsyncClient", mock_client_class),
        ):
            mock_service.get_session = AsyncMock(return_value=session_with_container)

            from api_server.routers.sessions import new_conversation

            await new_conversation(session_id=session_id, db=mock_db)

        call_args = mock_client_instance.post.call_args
        forwarded_headers = call_args.kwargs.get("headers", {})

        assert "X-Service-Token" in forwarded_headers, (
            f"POST to container-manager must include 'X-Service-Token' header.\n"
            f"Headers sent: {forwarded_headers!r}"
        )

        service_token_value = forwarded_headers["X-Service-Token"]
        assert service_token_value, "X-Service-Token header must not be empty."

    @pytest.mark.asyncio
    async def test_raise_for_status_is_called_on_proxy_response(self):
        """After forwarding, raise_for_status() must be called on the response.

        Without this call, a 4xx or 5xx from the container-manager would be
        swallowed silently and the caller would receive a false 204 success.
        """
        mock_db = AsyncMock()
        session_id = uuid4()
        container_id = "container-for-status-check"

        session_with_container = _make_mock_session(container_id=container_id)

        mock_response = AsyncMock()
        mock_response.raise_for_status = MagicMock()

        mock_client_class, mock_client_instance = _make_mock_httpx_client(mock_response)

        with (
            patch("api_server.routers.sessions.session_service") as mock_service,
            patch("api_server.routers.sessions.httpx.AsyncClient", mock_client_class),
        ):
            mock_service.get_session = AsyncMock(return_value=session_with_container)

            from api_server.routers.sessions import new_conversation

            await new_conversation(session_id=session_id, db=mock_db)

        mock_response.raise_for_status.assert_called_once()
