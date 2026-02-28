"""Tests for the cancel_agent_execution endpoint in routers.py.

Covers the happy path, agent-bridge error response, and connection
failure cases (OSError and WebSocketException). Uses the same mock
pattern as test_new_conversation.py.
"""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import websockets.exceptions
from fastapi import HTTPException

from container_manager.routers import cancel_agent_execution


def _make_mock_ws(recv_payload: dict) -> AsyncMock:
    """Build a mock WebSocket whose recv() returns the given payload as JSON."""
    mock_ws = AsyncMock()
    mock_ws.send = AsyncMock()
    mock_ws.recv = AsyncMock(return_value=json.dumps(recv_payload))
    return mock_ws


def _make_mock_connect(mock_ws: AsyncMock) -> MagicMock:
    """Wrap a mock WebSocket in a mock async context manager for websockets.connect."""
    mock_connect = MagicMock()
    mock_connect.return_value.__aenter__ = AsyncMock(return_value=mock_ws)
    mock_connect.return_value.__aexit__ = AsyncMock(return_value=False)
    return mock_connect


def _make_mock_docker(container_name: str = "agent-user1") -> AsyncMock:
    """Build a mock DockerClient whose get_container_name returns a fixed name."""
    mock_docker = AsyncMock()
    mock_docker.get_container_name = AsyncMock(return_value=container_name)
    return mock_docker


class TestCancelAgentExecution:
    """Test suite for the /containers/{container_id}/cancel endpoint."""

    @pytest.mark.asyncio
    async def test_sends_cancel_execution_jsonrpc(self):
        """Happy path: sends the correct JSON-RPC frame and returns without error."""
        mock_ws = _make_mock_ws(recv_payload={"result": {"success": True}, "done": True})
        mock_connect = _make_mock_connect(mock_ws)
        mock_docker = _make_mock_docker(container_name="agent-user1")

        with patch("container_manager.routers.websockets.connect", mock_connect):
            result = await cancel_agent_execution(
                container_id="ctr-123",
                docker=mock_docker,
            )

        # Function returns None (204 No Content).
        assert result is None

        # WebSocket opened against the correct URI.
        connect_uri = mock_connect.call_args[0][0]
        assert connect_uri == "ws://agent-user1:9100"

        # Exactly one send with the cancel_execution method.
        mock_ws.send.assert_called_once()
        raw_sent = mock_ws.send.call_args[0][0]
        sent_frame = json.loads(raw_sent)

        assert sent_frame["method"] == "cancel_execution"
        assert sent_frame["params"] == {}
        assert sent_frame["id"] == "cancel"

    @pytest.mark.asyncio
    async def test_returns_502_on_agent_bridge_error(self):
        """When agent-bridge replies with an error frame, raise HTTP 502."""
        mock_ws = _make_mock_ws(recv_payload={"error": "internal error"})
        mock_connect = _make_mock_connect(mock_ws)
        mock_docker = _make_mock_docker()

        with patch("container_manager.routers.websockets.connect", mock_connect):
            with pytest.raises(HTTPException) as exc_info:
                await cancel_agent_execution(
                    container_id="ctr-123",
                    docker=mock_docker,
                )

        assert exc_info.value.status_code == 502
        assert "internal error" in exc_info.value.detail

    @pytest.mark.asyncio
    async def test_returns_502_on_connection_failure(self):
        """When the WebSocket connection raises OSError, raise HTTP 502."""
        oserror = OSError("Connection refused")
        mock_connect = MagicMock()
        mock_connect.return_value.__aenter__ = AsyncMock(side_effect=oserror)
        mock_connect.return_value.__aexit__ = AsyncMock(return_value=False)
        mock_docker = _make_mock_docker()

        with patch("container_manager.routers.websockets.connect", mock_connect):
            with pytest.raises(HTTPException) as exc_info:
                await cancel_agent_execution(
                    container_id="ctr-123",
                    docker=mock_docker,
                )

        assert exc_info.value.status_code == 502
        assert "Cannot reach agent-bridge" in exc_info.value.detail

    @pytest.mark.asyncio
    async def test_returns_502_on_websocket_exception(self):
        """When websockets raises a WebSocketException, raise HTTP 502."""
        websocket_error = websockets.exceptions.WebSocketException("Handshake failed")
        mock_connect = MagicMock()
        mock_connect.return_value.__aenter__ = AsyncMock(side_effect=websocket_error)
        mock_connect.return_value.__aexit__ = AsyncMock(return_value=False)
        mock_docker = _make_mock_docker()

        with patch("container_manager.routers.websockets.connect", mock_connect):
            with pytest.raises(HTTPException) as exc_info:
                await cancel_agent_execution(
                    container_id="ctr-123",
                    docker=mock_docker,
                )

        assert exc_info.value.status_code == 502
        assert "Cannot reach agent-bridge" in exc_info.value.detail

    @pytest.mark.asyncio
    async def test_uses_short_timeout(self):
        """The WebSocket connect should use a short open_timeout for cancel."""
        mock_ws = _make_mock_ws(recv_payload={"result": {"success": True}, "done": True})
        mock_connect = _make_mock_connect(mock_ws)
        mock_docker = _make_mock_docker()

        with patch("container_manager.routers.websockets.connect", mock_connect):
            await cancel_agent_execution(
                container_id="ctr-123",
                docker=mock_docker,
            )

        call_kwargs = mock_connect.call_args[1]
        assert call_kwargs.get("open_timeout") == 5
