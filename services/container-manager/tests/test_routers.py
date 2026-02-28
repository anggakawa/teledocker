"""Tests for routers: _wait_for_agent_bridge readiness probe and WebSocket retry logic.

These tests mock websockets.connect to simulate the race condition between
container start and WebSocket server readiness, and the retry backoff for
transient connection failures in the message delivery path.
"""

import asyncio
import json
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from container_manager.routers import _wait_for_agent_bridge


class _AsyncFrameIterator:
    """Async iterator over a list of WebSocket frame strings.

    websockets uses `async for frame in ws:` to iterate incoming messages,
    so we need a proper async iterator — not a sync one.
    """

    def __init__(self, frames: list[str]):
        self._frames = iter(frames)

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            return next(self._frames)
        except StopIteration:
            raise StopAsyncIteration


class _FakeWebSocket:
    """Minimal async context manager mimicking a successful websockets connection."""

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass


# ---------------------------------------------------------------------------
# Tests: _wait_for_agent_bridge readiness probe
# ---------------------------------------------------------------------------


class TestWaitForAgentBridge:
    """Verify the readiness probe polls until the WS server is up."""

    @pytest.mark.asyncio
    async def test_returns_immediately_when_bridge_is_ready(self):
        """If the bridge accepts on the first try, return without delay."""
        with patch("container_manager.routers.websockets.connect") as mock_connect:
            mock_connect.return_value = _FakeWebSocket()

            await _wait_for_agent_bridge("test-container", timeout=5.0, interval=0.1)

            mock_connect.assert_called_once()
            call_args = mock_connect.call_args
            assert "ws://test-container:9100" in call_args[0]

    @pytest.mark.asyncio
    async def test_retries_until_bridge_becomes_ready(self):
        """Should keep polling when initial attempts fail, then succeed."""
        attempt_count = 0

        @asynccontextmanager
        async def mock_connect_factory(*args, **kwargs):
            nonlocal attempt_count
            attempt_count += 1
            if attempt_count < 3:
                raise ConnectionRefusedError("Connection refused")
            yield MagicMock()

        with patch("container_manager.routers.websockets.connect", side_effect=mock_connect_factory):
            with patch("container_manager.routers.asyncio.sleep", new_callable=AsyncMock):
                await _wait_for_agent_bridge(
                    "test-container", timeout=10.0, interval=0.1,
                )

        assert attempt_count == 3

    @pytest.mark.asyncio
    async def test_raises_timeout_when_bridge_never_ready(self):
        """Should raise TimeoutError if bridge never accepts within timeout."""

        @asynccontextmanager
        async def always_refuse(*args, **kwargs):
            raise ConnectionRefusedError("Connection refused")
            yield  # pragma: no cover

        # Use a very short timeout so the test finishes quickly.
        with patch("container_manager.routers.websockets.connect", side_effect=always_refuse):
            with patch("container_manager.routers.asyncio.sleep", new_callable=AsyncMock):
                # Override the event loop time to simulate rapid timeout.
                call_count = 0
                original_time = asyncio.get_event_loop().time

                def fake_time():
                    nonlocal call_count
                    call_count += 1
                    # First call sets deadline, subsequent calls exceed it after a few tries.
                    if call_count <= 4:
                        return 0.0
                    return 100.0

                with patch.object(asyncio.get_event_loop(), "time", side_effect=fake_time):
                    with pytest.raises(TimeoutError, match="not ready after"):
                        await _wait_for_agent_bridge(
                            "test-container", timeout=1.0, interval=0.1,
                        )

    @pytest.mark.asyncio
    async def test_timeout_error_includes_last_exception(self):
        """The TimeoutError message should include the last connection error."""

        @asynccontextmanager
        async def refuse_with_message(*args, **kwargs):
            raise OSError("No route to host")
            yield  # pragma: no cover

        call_count = 0
        original_time = asyncio.get_event_loop().time

        def fake_time():
            nonlocal call_count
            call_count += 1
            if call_count <= 3:
                return 0.0
            return 100.0

        with patch("container_manager.routers.websockets.connect", side_effect=refuse_with_message):
            with patch("container_manager.routers.asyncio.sleep", new_callable=AsyncMock):
                with patch.object(asyncio.get_event_loop(), "time", side_effect=fake_time):
                    with pytest.raises(TimeoutError, match="No route to host"):
                        await _wait_for_agent_bridge("test-container", timeout=1.0)


# ---------------------------------------------------------------------------
# Tests: WebSocket retry logic in generate()
# ---------------------------------------------------------------------------


class TestWebSocketRetryInGenerate:
    """Verify the retry-with-backoff logic in send_message_to_agent's generate()."""

    @pytest.mark.asyncio
    async def test_connects_on_first_attempt(self):
        """When WebSocket connects immediately, message is sent and response streamed."""
        mock_ws = AsyncMock()
        mock_ws.send = AsyncMock()
        mock_ws.close = AsyncMock()

        # Simulate a single response frame then done.
        response_frame = json.dumps({"event": {"type": "text_delta", "text": "Hi"}})
        done_frame = json.dumps({"done": True})
        mock_ws.__aiter__ = lambda self: _AsyncFrameIterator(
            [response_frame, done_frame],
        )

        with patch("container_manager.routers.websockets.connect", new_callable=AsyncMock) as mock_connect:
            mock_connect.return_value = mock_ws

            # Import after patching to get the real endpoint function.
            from container_manager.routers import SendMessageRequest, send_message_to_agent

            mock_docker = AsyncMock()
            mock_docker.get_container_name = AsyncMock(return_value="agent-user1")

            payload = SendMessageRequest(text="Hello", env_vars={})

            # Call the endpoint — it returns a StreamingResponse.
            response = await send_message_to_agent(
                container_id="ctr-123", payload=payload, docker=mock_docker,
            )

            # Consume the SSE stream.
            chunks = []
            async for chunk in response.body_iterator:
                chunks.append(chunk)

            mock_connect.assert_called_once()
            mock_ws.send.assert_called_once()
            assert any('"text_delta"' in c for c in chunks)

    @pytest.mark.asyncio
    async def test_retries_on_transient_oserror(self):
        """Should retry on OSError and succeed on the second attempt."""
        mock_ws = AsyncMock()
        mock_ws.send = AsyncMock()
        mock_ws.close = AsyncMock()

        done_frame = json.dumps({"done": True})
        mock_ws.__aiter__ = lambda self: _AsyncFrameIterator([done_frame])

        with patch("container_manager.routers.websockets.connect", new_callable=AsyncMock) as mock_connect:
            # First attempt fails, second succeeds.
            mock_connect.side_effect = [
                OSError("Connection refused"),
                mock_ws,
            ]

            with patch("container_manager.routers.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
                from container_manager.routers import SendMessageRequest, send_message_to_agent

                mock_docker = AsyncMock()
                mock_docker.get_container_name = AsyncMock(return_value="agent-user1")

                payload = SendMessageRequest(text="Hello", env_vars={})
                response = await send_message_to_agent(
                    container_id="ctr-123", payload=payload, docker=mock_docker,
                )

                chunks = []
                async for chunk in response.body_iterator:
                    chunks.append(chunk)

                assert mock_connect.call_count == 2
                # Backoff sleep was called between retries.
                mock_sleep.assert_called_once_with(0.5)

    @pytest.mark.asyncio
    async def test_gives_up_after_max_retries(self):
        """After max_retries failures, the error should be yielded as SSE."""
        with patch("container_manager.routers.websockets.connect", new_callable=AsyncMock) as mock_connect:
            mock_connect.side_effect = OSError("Connection refused")

            with patch("container_manager.routers.asyncio.sleep", new_callable=AsyncMock):
                from container_manager.routers import SendMessageRequest, send_message_to_agent

                mock_docker = AsyncMock()
                mock_docker.get_container_name = AsyncMock(return_value="agent-user1")

                payload = SendMessageRequest(text="Hello", env_vars={})
                response = await send_message_to_agent(
                    container_id="ctr-123", payload=payload, docker=mock_docker,
                )

                chunks = []
                async for chunk in response.body_iterator:
                    chunks.append(chunk)

                assert mock_connect.call_count == 3
                # Should contain an error SSE event.
                error_chunks = [c for c in chunks if "error" in c and "Connection refused" in c]
                assert len(error_chunks) >= 1
                # Should always end with [DONE].
                assert chunks[-1] == "data: [DONE]\n\n"
