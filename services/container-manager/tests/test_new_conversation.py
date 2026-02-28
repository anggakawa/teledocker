"""Tests for the new_conversation endpoint in routers.py.

Covers the happy path, agent-bridge error response, and connection
failure cases (OSError and WebSocketException).  The endpoint is called
directly — not via TestClient — so no HTTP layer is involved.

Mocking note: websockets.connect is used as `async with websockets.connect(uri) as ws:`.
That means calling connect() must return an async context manager — NOT a coroutine.
If we use AsyncMock for the callable, calling it returns a coroutine object, which
cannot be used as an async context manager and causes:
    TypeError: 'coroutine' object does not support the asynchronous context manager protocol

The correct pattern is MagicMock for the callable itself (so the call returns
mock_connect.return_value synchronously), combined with AsyncMock for __aenter__
and __aexit__ on that return_value.

For error-raising cases the exception is raised during the call to connect(),
before the context manager is entered, so AsyncMock(side_effect=...) is right.
"""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import websockets.exceptions
from fastapi import HTTPException

from container_manager.routers import new_conversation


def _make_mock_ws(recv_payload: dict) -> AsyncMock:
    """Build a mock WebSocket whose recv() returns the given payload as JSON."""
    mock_ws = AsyncMock()
    mock_ws.send = AsyncMock()
    mock_ws.recv = AsyncMock(return_value=json.dumps(recv_payload))
    return mock_ws


def _make_mock_connect(mock_ws: AsyncMock) -> MagicMock:
    """Wrap a mock WebSocket in a mock async context manager for websockets.connect.

    websockets.connect is called as `async with websockets.connect(uri) as ws:`.
    The production code calls connect(uri, open_timeout=10) and immediately uses
    the result as an async context manager.  Using MagicMock for the callable
    ensures that calling it returns mock_connect.return_value synchronously.
    Attaching AsyncMock __aenter__ and __aexit__ to that return_value satisfies
    the `async with` protocol.
    """
    mock_connect = MagicMock()
    mock_connect.return_value.__aenter__ = AsyncMock(return_value=mock_ws)
    mock_connect.return_value.__aexit__ = AsyncMock(return_value=False)
    return mock_connect


def _make_mock_docker(container_name: str = "agent-user1") -> AsyncMock:
    """Build a mock DockerClient whose get_container_name returns a fixed name."""
    mock_docker = AsyncMock()
    mock_docker.get_container_name = AsyncMock(return_value=container_name)
    return mock_docker


class TestNewConversation:
    """Test suite for the /containers/{container_id}/new-conversation endpoint."""

    @pytest.mark.asyncio
    async def test_sends_new_conversation_jsonrpc(self):
        """Happy path: sends the correct JSON-RPC frame and raises no exception.

        The agent-bridge replies with {"result": "ok"}, which is a success
        frame.  The endpoint should return None without raising.
        """
        # Arrange — build a mock WebSocket that returns a success frame.
        mock_ws = _make_mock_ws(recv_payload={"result": "ok"})
        mock_connect = _make_mock_connect(mock_ws)
        mock_docker = _make_mock_docker(container_name="agent-user1")

        with patch("container_manager.routers.websockets.connect", mock_connect):
            # Act — call the endpoint function directly.
            result = await new_conversation(
                container_id="ctr-123",
                docker=mock_docker,
            )

        # Assert — function returns None (204 No Content).
        assert result is None

        # Assert — WebSocket was opened against the correct URI.
        connect_uri = mock_connect.call_args[0][0]
        assert connect_uri == "ws://agent-user1:9100", (
            f"Expected URI 'ws://agent-user1:9100', got '{connect_uri}'"
        )

        # Assert — exactly one send call with the correct JSON-RPC payload.
        mock_ws.send.assert_called_once()
        raw_sent = mock_ws.send.call_args[0][0]
        sent_frame = json.loads(raw_sent)

        assert sent_frame["method"] == "new_conversation", (
            f"Expected method 'new_conversation', got '{sent_frame['method']}'"
        )
        assert sent_frame["params"] == {}, (
            f"Expected empty params dict, got {sent_frame['params']!r}"
        )
        assert sent_frame["id"] == "new-conv", f"Expected id 'new-conv', got '{sent_frame['id']}'"

    @pytest.mark.asyncio
    async def test_returns_502_on_agent_bridge_error(self):
        """When the agent-bridge replies with an error frame, raise HTTP 502.

        The endpoint checks for a truthy "error" key in the parsed frame and
        must propagate it as a 502 Bad Gateway.
        """
        # Arrange — agent-bridge replies with an error frame.
        mock_ws = _make_mock_ws(recv_payload={"error": "session already active"})
        mock_connect = _make_mock_connect(mock_ws)
        mock_docker = _make_mock_docker()

        with patch("container_manager.routers.websockets.connect", mock_connect):
            # Act & Assert — endpoint must raise HTTPException with 502.
            with pytest.raises(HTTPException) as exc_info:
                await new_conversation(
                    container_id="ctr-123",
                    docker=mock_docker,
                )

        raised_exception = exc_info.value
        assert raised_exception.status_code == 502, (
            f"Expected status 502, got {raised_exception.status_code}"
        )
        assert "session already active" in raised_exception.detail, (
            f"Expected agent error text in detail, got: {raised_exception.detail!r}"
        )

    @pytest.mark.asyncio
    async def test_returns_502_on_connection_failure(self):
        """When the WebSocket connection raises OSError, raise HTTP 502.

        This simulates the container being unreachable (e.g. not yet started,
        wrong network, or paused without agent-bridge running).

        Implementation note: `async with websockets.connect(uri) as ws:` means
        connect() is called synchronously and then __aenter__ is awaited.  The
        exception must be raised inside __aenter__ so that the `async with`
        block sees it.  We use MagicMock for the callable and set __aenter__
        to raise — AsyncMock(side_effect=...) would fail because calling an
        AsyncMock returns a coroutine object, not an async context manager.
        """
        # Arrange — build a mock connect() whose __aenter__ raises OSError.
        oserror = OSError("Connection refused")
        mock_connect = MagicMock()
        mock_connect.return_value.__aenter__ = AsyncMock(side_effect=oserror)
        mock_connect.return_value.__aexit__ = AsyncMock(return_value=False)
        mock_docker = _make_mock_docker()

        with patch("container_manager.routers.websockets.connect", mock_connect):
            # Act & Assert — endpoint must raise HTTPException with 502.
            with pytest.raises(HTTPException) as exc_info:
                await new_conversation(
                    container_id="ctr-123",
                    docker=mock_docker,
                )

        raised_exception = exc_info.value
        assert raised_exception.status_code == 502, (
            f"Expected status 502, got {raised_exception.status_code}"
        )
        assert "Cannot reach agent-bridge" in raised_exception.detail, (
            f"Expected 'Cannot reach agent-bridge' in detail, got: {raised_exception.detail!r}"
        )

    @pytest.mark.asyncio
    async def test_returns_502_on_websocket_exception(self):
        """When websockets raises a WebSocketException, raise HTTP 502.

        This covers protocol-level errors such as unexpected close frames or
        handshake failures, which are distinct from OS-level errors.

        Same mock pattern as test_returns_502_on_connection_failure: the
        exception is raised inside __aenter__, not at call time.
        """
        # Arrange — build a mock connect() whose __aenter__ raises WebSocketException.
        websocket_error = websockets.exceptions.WebSocketException("Handshake failed")
        mock_connect = MagicMock()
        mock_connect.return_value.__aenter__ = AsyncMock(side_effect=websocket_error)
        mock_connect.return_value.__aexit__ = AsyncMock(return_value=False)
        mock_docker = _make_mock_docker()

        with patch("container_manager.routers.websockets.connect", mock_connect):
            # Act & Assert — endpoint must raise HTTPException with 502.
            with pytest.raises(HTTPException) as exc_info:
                await new_conversation(
                    container_id="ctr-123",
                    docker=mock_docker,
                )

        raised_exception = exc_info.value
        assert raised_exception.status_code == 502, (
            f"Expected status 502, got {raised_exception.status_code}"
        )
        assert "Cannot reach agent-bridge" in raised_exception.detail, (
            f"Expected 'Cannot reach agent-bridge' in detail, got: {raised_exception.detail!r}"
        )
