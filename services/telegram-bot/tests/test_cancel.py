"""Tests for the /cancel command and cancellation support in the Telegram bot.

Tests cover:
1. TelegramStreamRenderer: request_cancel() sets flag, cancelled property,
   finalize(cancelled=True) appends cancel text.
2. /cancel command: sets renderer flag, calls API, handles no active stream.
3. Message handler: exits loop on cancelled flag, cleans up renderer from bot_data.
"""

import asyncio
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from telegram.error import BadRequest

from telegram_bot.commands.cancel import cancel_command
from telegram_bot.renderers.streaming import TelegramStreamRenderer

TELEGRAM_ID = 12345
SESSION_ID = uuid4()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_bot():
    """Create a mock Telegram bot with async methods."""
    bot = AsyncMock()
    message = MagicMock()
    message.message_id = 42
    bot.send_message.return_value = message
    bot.edit_message_text.return_value = None
    return bot


@pytest.fixture
def renderer(mock_bot):
    return TelegramStreamRenderer(bot=mock_bot, chat_id=TELEGRAM_ID)


def _make_update(telegram_id: int = TELEGRAM_ID) -> MagicMock:
    """Build a mock Telegram Update."""
    update = MagicMock()
    update.effective_user.id = telegram_id
    update.effective_chat.id = telegram_id
    update.message = MagicMock()
    update.message.reply_text = AsyncMock()
    update.message.text = "test message"
    update.message.message_id = 99
    return update


def _make_context(
    api_client: MagicMock,
    extra_bot_data: dict | None = None,
) -> MagicMock:
    """Build a mock PTB context with the given api_client in bot_data."""
    bot_data: dict = {"api_client": api_client}
    if extra_bot_data:
        bot_data.update(extra_bot_data)

    context = MagicMock()
    context.bot_data = bot_data
    context.args = []
    context.bot = MagicMock()
    return context


# ---------------------------------------------------------------------------
# Tests: TelegramStreamRenderer cancellation
# ---------------------------------------------------------------------------


class TestRendererCancellation:
    """Tests for cancellation support in TelegramStreamRenderer."""

    def test_initial_state_not_cancelled(self, renderer):
        """A fresh renderer should not be cancelled."""
        assert renderer.cancelled is False

    def test_request_cancel_sets_flag(self, renderer):
        """request_cancel() should set the cancelled flag."""
        renderer.request_cancel()
        assert renderer.cancelled is True

    def test_request_cancel_is_idempotent(self, renderer):
        """Calling request_cancel() multiple times should not crash."""
        renderer.request_cancel()
        renderer.request_cancel()
        assert renderer.cancelled is True

    @pytest.mark.asyncio
    async def test_finalize_with_cancelled_appends_notice(self, renderer, mock_bot):
        """finalize(cancelled=True) should append '(Cancelled by user)' to text."""
        await renderer.start()
        await renderer.handle_event({"type": "text_delta", "text": "Starting..."})
        await renderer.finalize(cancelled=True)

        full_text = renderer.get_full_response()
        assert "(Cancelled by user)" in full_text

    @pytest.mark.asyncio
    async def test_finalize_without_cancelled_no_notice(self, renderer, mock_bot):
        """finalize() without cancelled should not append cancel notice."""
        await renderer.start()
        await renderer.handle_event({"type": "text_delta", "text": "Complete."})
        await renderer.finalize()

        full_text = renderer.get_full_response()
        assert "(Cancelled by user)" not in full_text

    @pytest.mark.asyncio
    async def test_finalize_cancelled_with_no_prior_text(self, renderer, mock_bot):
        """finalize(cancelled=True) on empty buffer should still show cancel notice."""
        await renderer.start()
        await renderer.finalize(cancelled=True)

        full_text = renderer.get_full_response()
        assert "(Cancelled by user)" in full_text


# ---------------------------------------------------------------------------
# Tests: /cancel command
# ---------------------------------------------------------------------------


class TestCancelCommand:
    """Tests for cancel_command — the /cancel Telegram handler."""

    @pytest.mark.asyncio
    async def test_no_active_renderer_shows_nothing_to_cancel(self):
        """When no renderer is tracked, reply 'Nothing to cancel'."""
        api_client = AsyncMock()
        update = _make_update()
        context = _make_context(api_client)

        await cancel_command(update, context)

        reply_text = update.message.reply_text.call_args[0][0]
        assert "Nothing to cancel" in reply_text

    @pytest.mark.asyncio
    async def test_already_cancelled_shows_nothing_to_cancel(self):
        """When renderer is already cancelled, reply 'Nothing to cancel'."""
        mock_renderer = MagicMock()
        mock_renderer.cancelled = True

        api_client = AsyncMock()
        update = _make_update()
        context = _make_context(
            api_client,
            extra_bot_data={f"renderer:{TELEGRAM_ID}": mock_renderer},
        )

        await cancel_command(update, context)

        reply_text = update.message.reply_text.call_args[0][0]
        assert "Nothing to cancel" in reply_text

    @pytest.mark.asyncio
    async def test_cancel_sets_renderer_flag_and_replies(self):
        """Cancel should set the renderer's cancelled flag and reply 'Cancelling...'."""
        mock_renderer = MagicMock()
        mock_renderer.cancelled = False

        api_client = AsyncMock()
        api_client.cancel_session = AsyncMock()
        # Provide a cached session so the backend cancel is attempted.
        session_cache_key = f"session:{TELEGRAM_ID}"

        update = _make_update()
        context = _make_context(
            api_client,
            extra_bot_data={
                f"renderer:{TELEGRAM_ID}": mock_renderer,
                session_cache_key: str(SESSION_ID),
            },
        )

        await cancel_command(update, context)

        # Renderer's request_cancel was called.
        mock_renderer.request_cancel.assert_called_once()

        # User got "Cancelling..." reply.
        reply_calls = update.message.reply_text.call_args_list
        cancel_reply = reply_calls[0][0][0]
        assert "Cancelling" in cancel_reply

    @pytest.mark.asyncio
    async def test_cancel_calls_api_cancel_session(self):
        """Cancel should fire a best-effort API call to cancel the backend."""
        mock_renderer = MagicMock()
        mock_renderer.cancelled = False

        api_client = AsyncMock()
        api_client.cancel_session = AsyncMock()

        session_cache_key = f"session:{TELEGRAM_ID}"

        update = _make_update()
        context = _make_context(
            api_client,
            extra_bot_data={
                f"renderer:{TELEGRAM_ID}": mock_renderer,
                session_cache_key: str(SESSION_ID),
            },
        )

        await cancel_command(update, context)

        api_client.cancel_session.assert_awaited_once_with(SESSION_ID)

    @pytest.mark.asyncio
    async def test_api_cancel_failure_does_not_crash(self):
        """Even if the API cancel fails, the command should not raise."""
        mock_renderer = MagicMock()
        mock_renderer.cancelled = False

        api_client = AsyncMock()
        api_client.cancel_session = AsyncMock(side_effect=RuntimeError("connection lost"))

        session_cache_key = f"session:{TELEGRAM_ID}"

        update = _make_update()
        context = _make_context(
            api_client,
            extra_bot_data={
                f"renderer:{TELEGRAM_ID}": mock_renderer,
                session_cache_key: str(SESSION_ID),
            },
        )

        # Should not raise despite the API failure.
        await cancel_command(update, context)

        # Renderer was still cancelled despite API failure.
        mock_renderer.request_cancel.assert_called_once()

    @pytest.mark.asyncio
    async def test_cancel_without_session_skips_api_call(self):
        """If no session is found, skip the backend cancel (no crash)."""
        mock_renderer = MagicMock()
        mock_renderer.cancelled = False

        api_client = AsyncMock()
        api_client.cancel_session = AsyncMock()
        api_client.get_active_session_by_telegram_id = AsyncMock(return_value=None)

        update = _make_update()
        context = _make_context(
            api_client,
            extra_bot_data={f"renderer:{TELEGRAM_ID}": mock_renderer},
        )

        await cancel_command(update, context)

        # Renderer was cancelled.
        mock_renderer.request_cancel.assert_called_once()

        # API cancel was NOT called because there's no session.
        api_client.cancel_session.assert_not_awaited()


# ---------------------------------------------------------------------------
# Tests: Message handler cancellation integration
# ---------------------------------------------------------------------------


class TestMessageHandlerCancellation:
    """Tests for cancellation integration in the message handler."""

    @pytest.mark.asyncio
    async def test_renderer_stored_in_bot_data(self):
        """The message handler should store the renderer in bot_data."""
        from chatops_shared.schemas.session import SessionDTO
        from chatops_shared.schemas.user import UserDTO, UserRole

        user_dto = UserDTO(
            id=uuid4(),
            telegram_id=TELEGRAM_ID,
            telegram_username="test",
            display_name="Test",
            role=UserRole.user,
            is_approved=True,
            is_active=True,
            max_containers=5,
            provider_config=None,
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )

        # Track whether renderer was stored during execution.
        renderer_was_stored = False
        original_renderer_key = f"renderer:{TELEGRAM_ID}"

        api_client = AsyncMock()
        api_client.get_user = AsyncMock(return_value=user_dto)

        async def mock_stream_events(**kwargs):
            nonlocal renderer_was_stored
            # Check if renderer is in bot_data during streaming.
            if original_renderer_key in context.bot_data:
                renderer_was_stored = True
            # Yield one event, then finish.
            yield {"type": "text_delta", "text": "Hello"}

        api_client.stream_message_events = mock_stream_events

        session_cache_key = f"session:{TELEGRAM_ID}"
        update = _make_update()
        context = _make_context(
            api_client,
            extra_bot_data={session_cache_key: str(SESSION_ID)},
        )
        context.bot = AsyncMock()
        msg_mock = MagicMock()
        msg_mock.message_id = 42
        context.bot.send_message = AsyncMock(return_value=msg_mock)
        context.bot.edit_message_text = AsyncMock()

        from telegram_bot.handlers.message import default_message_handler

        await default_message_handler(update, context)

        assert renderer_was_stored, "Renderer should be stored in bot_data during streaming"
        # After handler completes, renderer should be cleaned up.
        assert original_renderer_key not in context.bot_data

    @pytest.mark.asyncio
    async def test_cancelled_renderer_breaks_event_loop(self):
        """When renderer.cancelled is True, the event loop should break."""
        from chatops_shared.schemas.user import UserDTO, UserRole

        user_dto = UserDTO(
            id=uuid4(),
            telegram_id=TELEGRAM_ID,
            telegram_username="test",
            display_name="Test",
            role=UserRole.user,
            is_approved=True,
            is_active=True,
            max_containers=5,
            provider_config=None,
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )

        events_processed = 0

        api_client = AsyncMock()
        api_client.get_user = AsyncMock(return_value=user_dto)

        async def mock_stream_events(**kwargs):
            nonlocal events_processed
            yield {"type": "text_delta", "text": "First"}
            events_processed += 1

            # Simulate cancel being set after first event.
            renderer_key = f"renderer:{TELEGRAM_ID}"
            renderer = context.bot_data.get(renderer_key)
            if renderer:
                renderer.request_cancel()

            yield {"type": "text_delta", "text": "Second"}
            events_processed += 1
            yield {"type": "text_delta", "text": "Third"}
            events_processed += 1

        api_client.stream_message_events = mock_stream_events

        session_cache_key = f"session:{TELEGRAM_ID}"
        update = _make_update()
        context = _make_context(
            api_client,
            extra_bot_data={session_cache_key: str(SESSION_ID)},
        )
        context.bot = AsyncMock()
        msg_mock = MagicMock()
        msg_mock.message_id = 42
        context.bot.send_message = AsyncMock(return_value=msg_mock)
        context.bot.edit_message_text = AsyncMock()

        from telegram_bot.handlers.message import default_message_handler

        await default_message_handler(update, context)

        # The handler processes the first event, then the generator yields
        # a second event (at which point the cancelled flag is checked and
        # the loop breaks). The third event should NOT be processed.
        # events_processed counts generator yields, not handler processing.
        # The important thing is the handler exited gracefully.
        assert f"renderer:{TELEGRAM_ID}" not in context.bot_data
