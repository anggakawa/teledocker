"""Tests for main.py — singleton factories and JSON-RPC command routing.

Tests verify that:
- get_sdk_runner() and get_legacy_runner() return the same instance (singleton).
- clear_session, get_conversation, and new_conversation methods dispatch correctly.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agent_bridge import main


@pytest.fixture(autouse=True)
def _reset_singletons():
    """Reset module-level singletons before each test to ensure isolation."""
    main._sdk_runner = None
    main._legacy_runner = None
    yield
    main._sdk_runner = None
    main._legacy_runner = None


class TestSingletonFactories:
    """Tests for get_sdk_runner and get_legacy_runner lazy singletons."""

    def test_get_sdk_runner_returns_singleton(self):
        """Calling get_sdk_runner twice should return the same instance."""
        with patch("agent_bridge.main.ClaudeSDKRunner") as mock_cls:
            mock_cls.return_value = MagicMock()
            first = main.get_sdk_runner()
            second = main.get_sdk_runner()

        assert first is second
        # Constructor called exactly once.
        mock_cls.assert_called_once()

    def test_get_legacy_runner_returns_singleton(self):
        """Calling get_legacy_runner twice should return the same instance."""
        with patch("agent_bridge.main.ClaudeCodeRunner") as mock_cls:
            mock_cls.return_value = MagicMock()
            first = main.get_legacy_runner()
            second = main.get_legacy_runner()

        assert first is second
        mock_cls.assert_called_once()

    def test_sdk_and_legacy_are_independent(self):
        """SDK runner and legacy runner should be separate instances."""
        with (
            patch("agent_bridge.main.ClaudeSDKRunner") as mock_sdk,
            patch("agent_bridge.main.ClaudeCodeRunner") as mock_legacy,
        ):
            mock_sdk.return_value = MagicMock(name="sdk")
            mock_legacy.return_value = MagicMock(name="legacy")

            sdk = main.get_sdk_runner()
            legacy = main.get_legacy_runner()

        assert sdk is not legacy


class TestClearSessionMethod:
    """Tests for the clear_session JSON-RPC method."""

    @pytest.mark.asyncio
    async def test_clear_session_calls_runner(self):
        """clear_session method should call sdk_runner.clear_session()."""
        mock_sdk_runner = MagicMock()
        mock_ws = AsyncMock()

        await main.dispatch_request(
            websocket=mock_ws,
            sdk_runner=mock_sdk_runner,
            legacy_runner=MagicMock(),
            method="clear_session",
            params={},
            request_id="req-1",
        )

        mock_sdk_runner.clear_session.assert_called_once()
        # Verify response was sent.
        mock_ws.send.assert_called_once()
        import json
        response = json.loads(mock_ws.send.call_args[0][0])
        assert response["id"] == "req-1"
        assert response["result"]["success"] is True
        assert response["done"] is True


class TestGetConversationMethod:
    """Tests for the get_conversation JSON-RPC method."""

    @pytest.mark.asyncio
    async def test_get_conversation_returns_session_info(self):
        """get_conversation should return the runner's session info."""
        mock_sdk_runner = MagicMock()
        mock_sdk_runner.get_session_info.return_value = {
            "session_id": "test-session",
            "has_session": True,
            "session_file": "/home/chatops/.agent_session",
        }
        mock_ws = AsyncMock()

        await main.dispatch_request(
            websocket=mock_ws,
            sdk_runner=mock_sdk_runner,
            legacy_runner=MagicMock(),
            method="get_conversation",
            params={},
            request_id="req-2",
        )

        mock_sdk_runner.get_session_info.assert_called_once()
        import json
        response = json.loads(mock_ws.send.call_args[0][0])
        assert response["result"]["session_id"] == "test-session"
        assert response["result"]["has_session"] is True


class TestNewConversationMethod:
    """Tests for the new_conversation JSON-RPC method."""

    @pytest.mark.asyncio
    async def test_new_conversation_clears_and_responds(self):
        """new_conversation should clear session and return success."""
        mock_sdk_runner = MagicMock()
        mock_ws = AsyncMock()

        await main.dispatch_request(
            websocket=mock_ws,
            sdk_runner=mock_sdk_runner,
            legacy_runner=MagicMock(),
            method="new_conversation",
            params={},
            request_id="req-3",
        )

        mock_sdk_runner.clear_session.assert_called_once()
        import json
        response = json.loads(mock_ws.send.call_args[0][0])
        assert response["result"]["success"] is True
        assert "New conversation" in response["result"]["message"]


class TestHandleConnectionUsesSingletons:
    """Tests that handle_connection uses the singleton factories."""

    @pytest.mark.asyncio
    async def test_handle_connection_calls_factories(self):
        """handle_connection should get runners from singleton factories."""
        mock_sdk = MagicMock()
        mock_legacy = MagicMock()

        with (
            patch("agent_bridge.main.get_sdk_runner", return_value=mock_sdk) as sdk_factory,
            patch("agent_bridge.main.get_legacy_runner", return_value=mock_legacy) as legacy_factory,
        ):
            # Create a mock websocket that yields no messages (empty iteration).
            mock_ws = AsyncMock()
            mock_ws.remote_address = ("127.0.0.1", 1234)
            mock_ws.__aiter__ = MagicMock(return_value=AsyncMock(__anext__=AsyncMock(side_effect=StopAsyncIteration)))

            await main.handle_connection(mock_ws)

        sdk_factory.assert_called_once()
        legacy_factory.assert_called_once()
