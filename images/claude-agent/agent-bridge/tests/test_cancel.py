"""Tests for cancellation support in ClaudeSDKRunner and agent-bridge dispatch.

Tests cover:
- cancel() sets the _cancel_event.
- send_message() clears _cancel_event before acquiring the lock.
- _run_query() yields a cancellation error and stops when _cancel_event is set.
- Lock is released after cancellation so subsequent messages work.
- cancel_execution JSON-RPC method calls sdk_runner.cancel().
"""

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agent_bridge import main


# ---------------------------------------------------------------------------
# SDK Runner fixtures
# ---------------------------------------------------------------------------


class FakeStreamEvent:
    """Mimics claude_agent_sdk.types.StreamEvent."""

    def __init__(self, event: dict, session_id: str = ""):
        self.event = event
        self.session_id = session_id


def _get_patched_runner(tmp_path: Path | None = None):
    """Create a ClaudeSDKRunner with session file pointed at a temp directory."""
    import tempfile

    from agent_bridge.sdk_runner import ClaudeSDKRunner

    if tmp_path is None:
        tmp_path = Path(tempfile.mkdtemp())

    fake_file = tmp_path / ".agent_session"
    with patch("agent_bridge.sdk_runner._SESSION_FILE", fake_file):
        runner = ClaudeSDKRunner()
        return runner


def _make_patches(mock_query_fn):
    """Create the standard set of patches for SDK runner tests."""
    return [
        patch("agent_bridge.sdk_runner.query", side_effect=mock_query_fn),
        patch("agent_bridge.sdk_runner.StreamEvent", FakeStreamEvent),
        patch("agent_bridge.sdk_runner.ClaudeAgentOptions", MagicMock),
    ]


async def _collect_events(runner, prompt: str, env_vars: dict | None = None) -> list[dict]:
    """Helper to collect all events from a runner.send_message call."""
    events = []
    async for event in runner.send_message(prompt, env_vars or {}):
        events.append(event)
    return events


# ---------------------------------------------------------------------------
# Tests: ClaudeSDKRunner cancellation
# ---------------------------------------------------------------------------


class TestSDKRunnerCancel:
    """Tests for the cooperative cancellation mechanism in ClaudeSDKRunner."""

    def test_cancel_sets_event(self, tmp_path):
        """cancel() should set the _cancel_event flag."""
        runner = _get_patched_runner(tmp_path)
        assert not runner._cancel_event.is_set()

        runner.cancel()
        assert runner._cancel_event.is_set()

    @pytest.mark.asyncio
    async def test_send_message_clears_cancel_event(self, tmp_path):
        """send_message() should clear _cancel_event before starting the query.

        A stale cancel from a previous request must not affect the new one.
        """
        runner = _get_patched_runner(tmp_path)
        runner.cancel()  # Simulate leftover cancel.
        assert runner._cancel_event.is_set()

        async def mock_query(prompt, options):
            # By the time query runs, cancel_event should be cleared.
            yield FakeStreamEvent({"type": "message_start"}, session_id="s1")

        patches = _make_patches(mock_query)
        for p in patches:
            p.start()
        try:
            events = await _collect_events(runner, "Hello")
        finally:
            for p in patches:
                p.stop()

        # The stale cancel was cleared, so no error event from cancellation.
        cancel_errors = [
            e for e in events
            if e["type"] == "error" and "Cancelled" in e.get("text", "")
        ]
        assert len(cancel_errors) == 0

    @pytest.mark.asyncio
    async def test_cancel_during_query_yields_error(self, tmp_path):
        """When cancel() is called during a running query, the next event
        checkpoint should yield a cancellation error and stop.
        """
        runner = _get_patched_runner(tmp_path)

        async def mock_query(prompt, options):
            yield FakeStreamEvent(
                {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "Hello"}},
                session_id="s1",
            )
            # Simulate the cancel being set between events.
            runner.cancel()
            yield FakeStreamEvent(
                {"type": "content_block_delta", "delta": {"type": "text_delta", "text": " world"}},
            )
            # This event should never be reached.
            yield FakeStreamEvent(
                {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "!"}},
            )

        patches = _make_patches(mock_query)
        for p in patches:
            p.start()
        try:
            events = await _collect_events(runner, "Test")
        finally:
            for p in patches:
                p.stop()

        # Should have: text_delta("Hello"), then cancel error, then timing result.
        text_events = [e for e in events if e["type"] == "text_delta"]
        assert len(text_events) == 1
        assert text_events[0]["text"] == "Hello"

        cancel_errors = [
            e for e in events
            if e["type"] == "error" and "Cancelled" in e.get("text", "")
        ]
        assert len(cancel_errors) == 1

    @pytest.mark.asyncio
    async def test_lock_released_after_cancel(self, tmp_path):
        """After cancellation, the lock should be released so new messages work."""
        runner = _get_patched_runner(tmp_path)

        call_count = 0

        async def mock_query(prompt, options):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                # First call — cancel immediately.
                runner.cancel()
                yield FakeStreamEvent({"type": "message_start"}, session_id="s1")
            else:
                # Second call should succeed normally.
                yield FakeStreamEvent({"type": "message_start"}, session_id="s2")
                yield FakeStreamEvent(
                    {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "OK"}},
                )

        patches = _make_patches(mock_query)
        for p in patches:
            p.start()
        try:
            # First call gets cancelled.
            events1 = await _collect_events(runner, "first")

            # Second call should work — lock was released.
            events2 = await _collect_events(runner, "second")
        finally:
            for p in patches:
                p.stop()

        assert call_count == 2

        # First call should have cancellation error.
        cancel_errors = [
            e for e in events1
            if e["type"] == "error" and "Cancelled" in e.get("text", "")
        ]
        assert len(cancel_errors) == 1

        # Second call should have normal text.
        text_events = [e for e in events2 if e["type"] == "text_delta"]
        assert len(text_events) == 1
        assert text_events[0]["text"] == "OK"

    @pytest.mark.asyncio
    async def test_cancel_is_idempotent(self, tmp_path):
        """Calling cancel() multiple times should not crash."""
        runner = _get_patched_runner(tmp_path)
        runner.cancel()
        runner.cancel()
        runner.cancel()
        assert runner._cancel_event.is_set()


# ---------------------------------------------------------------------------
# Tests: cancel_execution JSON-RPC dispatch
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_singletons():
    """Reset module-level singletons before each test."""
    main._sdk_runner = None
    main._legacy_runner = None
    yield
    main._sdk_runner = None
    main._legacy_runner = None


class TestCancelExecutionDispatch:
    """Tests for the cancel_execution JSON-RPC method in main.py dispatch."""

    @pytest.mark.asyncio
    async def test_cancel_execution_calls_runner_cancel(self):
        """cancel_execution method should call sdk_runner.cancel()."""
        mock_sdk_runner = MagicMock()
        mock_ws = AsyncMock()

        await main.dispatch_request(
            websocket=mock_ws,
            sdk_runner=mock_sdk_runner,
            legacy_runner=MagicMock(),
            method="cancel_execution",
            params={},
            request_id="cancel-1",
        )

        mock_sdk_runner.cancel.assert_called_once()

    @pytest.mark.asyncio
    async def test_cancel_execution_sends_success_response(self):
        """cancel_execution should respond with success=True and done=True."""
        import json

        mock_sdk_runner = MagicMock()
        mock_ws = AsyncMock()

        await main.dispatch_request(
            websocket=mock_ws,
            sdk_runner=mock_sdk_runner,
            legacy_runner=MagicMock(),
            method="cancel_execution",
            params={},
            request_id="cancel-2",
        )

        mock_ws.send.assert_called_once()
        response = json.loads(mock_ws.send.call_args[0][0])
        assert response["id"] == "cancel-2"
        assert response["result"]["success"] is True
        assert response["done"] is True
        assert "Cancellation" in response["result"]["message"]
