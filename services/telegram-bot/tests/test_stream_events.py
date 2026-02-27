"""Tests for ApiClient.stream_message_events() — structured event parsing.

Verifies that the SSE stream from api-server is correctly parsed into
structured event dicts, handling both new event format and legacy chunks.
"""

import json
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID

import pytest

from telegram_bot.api_client import ApiClient


def _make_sse_lines(payloads: list[dict | str]) -> list[str]:
    """Build SSE data lines from a list of payloads.

    Each payload becomes a 'data: {...}' line.
    A final '[DONE]' sentinel is appended.
    """
    lines = []
    for payload in payloads:
        if isinstance(payload, str):
            lines.append(f"data: {payload}")
        else:
            lines.append(f"data: {json.dumps(payload)}")
    lines.append("data: [DONE]")
    return lines


class FakeStreamResponse:
    """Fake httpx streaming response that yields SSE lines."""

    def __init__(self, lines: list[str]):
        self._lines = lines
        self.status_code = 200

    def raise_for_status(self):
        pass

    async def aiter_lines(self):
        for line in self._lines:
            yield line


class FakeClient:
    """Fake httpx.AsyncClient that returns a FakeStreamResponse."""

    def __init__(self, response: FakeStreamResponse):
        self._response = response

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    def stream(self, method, url, **kwargs):
        return FakeStreamContext(self._response)


class FakeStreamContext:
    def __init__(self, response):
        self._response = response

    async def __aenter__(self):
        return self._response

    async def __aexit__(self, *args):
        pass


@pytest.fixture
def api_client():
    return ApiClient(base_url="http://localhost:8000", service_token="test-token")


class TestStreamMessageEvents:
    """Tests for stream_message_events with structured events."""

    @pytest.mark.asyncio
    async def test_text_delta_events(self, api_client):
        """Text delta events should be yielded as-is."""
        lines = _make_sse_lines([
            {"event": {"type": "text_delta", "text": "Hello "}},
            {"event": {"type": "text_delta", "text": "world!"}},
        ])

        response = FakeStreamResponse(lines)
        with patch("httpx.AsyncClient", return_value=FakeClient(response)):
            events = []
            async for event in api_client.stream_message_events(
                UUID("00000000-0000-0000-0000-000000000001"), "test"
            ):
                events.append(event)

        assert len(events) == 2
        assert events[0] == {"type": "text_delta", "text": "Hello "}
        assert events[1] == {"type": "text_delta", "text": "world!"}

    @pytest.mark.asyncio
    async def test_tool_lifecycle_events(self, api_client):
        """Tool start/end/result events should pass through."""
        lines = _make_sse_lines([
            {"event": {"type": "tool_start", "tool_name": "Read"}},
            {"event": {"type": "tool_end", "tool_name": "Read", "tool_input": {"file_path": "x.py"}}},
            {"event": {"type": "tool_result", "tool_name": "Read", "tool_result_summary": "ok", "is_error": False}},
        ])

        response = FakeStreamResponse(lines)
        with patch("httpx.AsyncClient", return_value=FakeClient(response)):
            events = []
            async for event in api_client.stream_message_events(
                UUID("00000000-0000-0000-0000-000000000001"), "test"
            ):
                events.append(event)

        assert len(events) == 3
        assert events[0]["type"] == "tool_start"
        assert events[1]["type"] == "tool_end"
        assert events[1]["tool_input"] == {"file_path": "x.py"}
        assert events[2]["type"] == "tool_result"

    @pytest.mark.asyncio
    async def test_error_event(self, api_client):
        """Error events should be yielded."""
        lines = _make_sse_lines([
            {"event": {"type": "error", "text": "Something broke"}},
        ])

        response = FakeStreamResponse(lines)
        with patch("httpx.AsyncClient", return_value=FakeClient(response)):
            events = []
            async for event in api_client.stream_message_events(
                UUID("00000000-0000-0000-0000-000000000001"), "test"
            ):
                events.append(event)

        assert len(events) == 1
        assert events[0]["type"] == "error"
        assert events[0]["text"] == "Something broke"

    @pytest.mark.asyncio
    async def test_result_event(self, api_client):
        """Result events with session metadata should pass through."""
        lines = _make_sse_lines([
            {"event": {"type": "text_delta", "text": "Done."}},
            {"event": {"type": "result", "session_id": "abc", "duration_ms": 1500}},
        ])

        response = FakeStreamResponse(lines)
        with patch("httpx.AsyncClient", return_value=FakeClient(response)):
            events = []
            async for event in api_client.stream_message_events(
                UUID("00000000-0000-0000-0000-000000000001"), "test"
            ):
                events.append(event)

        assert len(events) == 2
        assert events[1]["type"] == "result"
        assert events[1]["session_id"] == "abc"

    @pytest.mark.asyncio
    async def test_legacy_chunk_fallback(self, api_client):
        """Legacy chunk format should be wrapped as legacy_chunk events."""
        lines = _make_sse_lines([
            {"chunk": "old format response"},
        ])

        response = FakeStreamResponse(lines)
        with patch("httpx.AsyncClient", return_value=FakeClient(response)):
            events = []
            async for event in api_client.stream_message_events(
                UUID("00000000-0000-0000-0000-000000000001"), "test"
            ):
                events.append(event)

        assert len(events) == 1
        assert events[0]["type"] == "legacy_chunk"
        assert events[0]["chunk"] == "old format response"

    @pytest.mark.asyncio
    async def test_error_from_server(self, api_client):
        """Server error in SSE should be wrapped as error event."""
        lines = _make_sse_lines([
            {"error": "container unreachable"},
        ])

        response = FakeStreamResponse(lines)
        with patch("httpx.AsyncClient", return_value=FakeClient(response)):
            events = []
            async for event in api_client.stream_message_events(
                UUID("00000000-0000-0000-0000-000000000001"), "test"
            ):
                events.append(event)

        assert len(events) == 1
        assert events[0]["type"] == "error"
        assert events[0]["text"] == "container unreachable"

    @pytest.mark.asyncio
    async def test_invalid_json_becomes_legacy_chunk(self, api_client):
        """Malformed JSON should be yielded as a legacy_chunk."""
        lines = ["data: not-valid-json", "data: [DONE]"]

        response = FakeStreamResponse(lines)
        with patch("httpx.AsyncClient", return_value=FakeClient(response)):
            events = []
            async for event in api_client.stream_message_events(
                UUID("00000000-0000-0000-0000-000000000001"), "test"
            ):
                events.append(event)

        assert len(events) == 1
        assert events[0]["type"] == "legacy_chunk"


class TestStreamMessageLegacy:
    """Tests for the legacy stream_message() method — backward compat."""

    @pytest.mark.asyncio
    async def test_extracts_text_from_events(self, api_client):
        """Legacy stream_message should extract text from text_delta events."""
        lines = _make_sse_lines([
            {"event": {"type": "text_delta", "text": "Hello"}},
            {"event": {"type": "tool_start", "tool_name": "Read"}},
            {"event": {"type": "text_delta", "text": " world"}},
        ])

        response = FakeStreamResponse(lines)
        with patch("httpx.AsyncClient", return_value=FakeClient(response)):
            chunks = []
            async for chunk in api_client.stream_message(
                UUID("00000000-0000-0000-0000-000000000001"), "test"
            ):
                chunks.append(chunk)

        # Should only yield text, not tool events.
        assert chunks == ["Hello", " world"]

    @pytest.mark.asyncio
    async def test_extracts_text_from_legacy_chunks(self, api_client):
        """Legacy stream_message should pass through legacy chunk format."""
        lines = _make_sse_lines([
            {"chunk": "old response"},
        ])

        response = FakeStreamResponse(lines)
        with patch("httpx.AsyncClient", return_value=FakeClient(response)):
            chunks = []
            async for chunk in api_client.stream_message(
                UUID("00000000-0000-0000-0000-000000000001"), "test"
            ):
                chunks.append(chunk)

        assert chunks == ["old response"]
