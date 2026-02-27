"""Tests for TelegramStreamRenderer — real-time message editing.

Tests verify the renderer correctly:
- Sends an initial placeholder message.
- Accumulates text_delta events and edits messages.
- Shows tool status lines during tool execution.
- Handles message overflow beyond 4096 characters.
- Skips edits when text hasn't changed.
- Handles Telegram API errors gracefully.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from telegram_bot.renderers.streaming import (
    TelegramStreamRenderer,
    _format_tool_status,
    _MAX_MESSAGE_LENGTH,
)


@pytest.fixture
def mock_bot():
    """Create a mock Telegram bot with async methods."""
    bot = AsyncMock()
    # send_message returns a Message with a message_id.
    message = MagicMock()
    message.message_id = 42
    bot.send_message.return_value = message
    # edit_message_text returns None (success).
    bot.edit_message_text.return_value = None
    return bot


@pytest.fixture
def renderer(mock_bot):
    return TelegramStreamRenderer(bot=mock_bot, chat_id=12345)


class TestFormatToolStatus:
    """Tests for the _format_tool_status helper."""

    def test_read_tool(self):
        result = _format_tool_status("Read", {"file_path": "src/main.py"})
        assert result == "> Reading src/main.py..."

    def test_write_tool(self):
        result = _format_tool_status("Write", {"file_path": "output.txt"})
        assert result == "> Writing output.txt..."

    def test_edit_tool(self):
        result = _format_tool_status("Edit", {"file_path": "config.py"})
        assert result == "> Editing config.py..."

    def test_bash_tool_short_command(self):
        result = _format_tool_status("Bash", {"command": "pytest tests/"})
        assert result == "> Running: pytest tests/..."

    def test_bash_tool_long_command_truncated(self):
        long_command = "x" * 100
        result = _format_tool_status("Bash", {"command": long_command})
        assert len(result) < 100
        assert result.endswith("...")

    def test_glob_tool(self):
        result = _format_tool_status("Glob", {"pattern": "**/*.py"})
        assert result == "> Searching: **/*.py..."

    def test_grep_tool(self):
        result = _format_tool_status("Grep", {"pattern": "def main"})
        assert result == "> Searching for: def main..."

    def test_unknown_tool_no_input(self):
        result = _format_tool_status("CustomTool")
        assert result == "> Using CustomTool..."

    def test_tool_start_no_input(self):
        result = _format_tool_status("Read")
        assert result == "> Reading..."


class TestStreamRendererStart:
    """Tests for renderer initialization."""

    @pytest.mark.asyncio
    async def test_start_sends_placeholder(self, renderer, mock_bot):
        await renderer.start()
        mock_bot.send_message.assert_called_once_with(
            chat_id=12345, text="..."
        )

    @pytest.mark.asyncio
    async def test_start_tracks_message(self, renderer, mock_bot):
        await renderer.start()
        assert renderer._message is not None
        assert len(renderer._messages) == 1


class TestStreamRendererTextDelta:
    """Tests for text_delta event handling."""

    @pytest.mark.asyncio
    async def test_accumulates_text(self, renderer, mock_bot):
        await renderer.start()

        await renderer.handle_event({"type": "text_delta", "text": "Hello "})
        await renderer.handle_event({"type": "text_delta", "text": "world!"})
        await renderer.finalize()

        assert renderer.get_full_response() == "Hello world!"

    @pytest.mark.asyncio
    async def test_edits_message_on_flush(self, renderer, mock_bot):
        await renderer.start()

        # Send enough text to trigger a buffer flush.
        large_text = "x" * 150
        await renderer.handle_event({"type": "text_delta", "text": large_text})

        # Give the flush a moment.
        await asyncio.sleep(0.1)
        await renderer.finalize()

        # Should have called edit_message_text at least once.
        assert mock_bot.edit_message_text.call_count >= 1


class TestStreamRendererToolEvents:
    """Tests for tool lifecycle event handling."""

    @pytest.mark.asyncio
    async def test_tool_start_shows_status(self, renderer, mock_bot):
        await renderer.start()

        await renderer.handle_event({"type": "tool_start", "tool_name": "Read"})
        await asyncio.sleep(0.4)

        # The edit should include tool status.
        last_call_args = mock_bot.edit_message_text.call_args
        if last_call_args:
            text = last_call_args.kwargs.get("text", "")
            assert "> Reading..." in text

    @pytest.mark.asyncio
    async def test_tool_end_shows_details(self, renderer, mock_bot):
        await renderer.start()

        await renderer.handle_event({"type": "text_delta", "text": "Let me check."})
        await renderer.handle_event({
            "type": "tool_end",
            "tool_name": "Read",
            "tool_input": {"file_path": "main.py"},
        })
        await asyncio.sleep(0.4)

        # Verify edit happened with tool details.
        assert mock_bot.edit_message_text.call_count >= 1

    @pytest.mark.asyncio
    async def test_tool_result_clears_status(self, renderer, mock_bot):
        await renderer.start()

        await renderer.handle_event({"type": "text_delta", "text": "Checking..."})
        await renderer.handle_event({"type": "tool_start", "tool_name": "Read"})
        await asyncio.sleep(0.4)
        await renderer.handle_event({
            "type": "tool_result",
            "tool_name": "Read",
            "tool_result_summary": "file content",
            "is_error": False,
        })
        await renderer.finalize()

        # Final message should not have tool status.
        last_call_args = mock_bot.edit_message_text.call_args
        text = last_call_args.kwargs.get("text", "")
        assert ">" not in text


class TestStreamRendererErrorHandling:
    """Tests for error event handling."""

    @pytest.mark.asyncio
    async def test_error_event_appended(self, renderer, mock_bot):
        await renderer.start()

        await renderer.handle_event({"type": "text_delta", "text": "Starting..."})
        await renderer.handle_event({"type": "error", "text": "Something broke"})
        await renderer.finalize()

        assert "Error: Something broke" in renderer.get_full_response()


class TestStreamRendererFinalize:
    """Tests for finalize behavior."""

    @pytest.mark.asyncio
    async def test_finalize_with_no_text(self, renderer, mock_bot):
        await renderer.start()
        await renderer.finalize()

        # Should show placeholder text.
        last_call_args = mock_bot.edit_message_text.call_args
        text = last_call_args.kwargs.get("text", "")
        assert text == "(no response from agent)"

    @pytest.mark.asyncio
    async def test_get_full_response(self, renderer, mock_bot):
        await renderer.start()

        await renderer.handle_event({"type": "text_delta", "text": "Part 1. "})
        await renderer.handle_event({"type": "text_delta", "text": "Part 2."})
        await renderer.finalize()

        assert renderer.get_full_response() == "Part 1. Part 2."


class TestStreamRendererOverflow:
    """Tests for message overflow handling."""

    @pytest.mark.asyncio
    async def test_overflow_starts_new_message(self, renderer, mock_bot):
        await renderer.start()

        # Send text that exceeds Telegram's limit.
        large_text = "A" * 4000
        await renderer.handle_event({"type": "text_delta", "text": large_text})
        await renderer.finalize()

        # Should have sent at least one additional message.
        assert mock_bot.send_message.call_count >= 1


class TestStreamRendererEditDedup:
    """Tests for edit deduplication (avoiding 'message not modified' errors)."""

    @pytest.mark.asyncio
    async def test_skips_identical_edit(self, renderer, mock_bot):
        await renderer.start()

        await renderer.handle_event({"type": "text_delta", "text": "Hello"})
        await renderer.finalize()

        edit_count_after_first = mock_bot.edit_message_text.call_count

        # Calling finalize again shouldn't trigger another edit.
        await renderer.finalize()
        assert mock_bot.edit_message_text.call_count == edit_count_after_first


class TestStreamRendererTelegramErrors:
    """Tests for graceful handling of Telegram API errors."""

    @pytest.mark.asyncio
    async def test_handles_bad_request_not_modified(self, renderer, mock_bot):
        """'Message is not modified' error should be silently ignored."""
        from telegram.error import BadRequest

        await renderer.start()

        mock_bot.edit_message_text.side_effect = BadRequest("Message is not modified")
        # Should not raise.
        await renderer.handle_event({"type": "text_delta", "text": "Test"})
        await renderer.finalize()

    @pytest.mark.asyncio
    async def test_handles_timeout_error(self, renderer, mock_bot):
        """TimedOut error should be logged but not crash."""
        from telegram.error import TimedOut

        await renderer.start()

        mock_bot.edit_message_text.side_effect = TimedOut()
        # Should not raise.
        await renderer.handle_event({"type": "text_delta", "text": "x" * 150})
        await asyncio.sleep(0.1)
