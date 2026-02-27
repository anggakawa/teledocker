"""Tests for session creation and message handler error guards.

Verifies that:
1. new_command does not cache a session whose status is "error" — a broken
   container must never be stored in bot_data or the user gets stuck.
2. default_message_handler clears the cached session ID when streaming raises
   an exception — so the user can recover with /new instead of being stuck in
   a loop.
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from chatops_shared.schemas.session import SessionDTO, SessionStatus
from chatops_shared.schemas.user import UserDTO, UserRole

from telegram_bot.commands.session import new_command
from telegram_bot.handlers.message import default_message_handler


# ---------------------------------------------------------------------------
# Shared test constants
# ---------------------------------------------------------------------------

TELEGRAM_ID = 12345
USER_ID = uuid4()
SESSION_ID = uuid4()


# ---------------------------------------------------------------------------
# Helper factories
# ---------------------------------------------------------------------------


def _now() -> datetime:
    """Return the current UTC time."""
    return datetime.now(timezone.utc)


def _make_user_dto(
    telegram_id: int = TELEGRAM_ID,
    is_approved: bool = True,
) -> UserDTO:
    """Build a minimal UserDTO for the given Telegram user."""
    return UserDTO(
        id=USER_ID,
        telegram_id=telegram_id,
        telegram_username="testuser",
        display_name="Test User",
        role=UserRole.user,
        is_approved=is_approved,
        is_active=True,
        max_containers=5,
        provider_config=None,
        created_at=_now(),
        updated_at=_now(),
    )


def _make_session_dto(
    status: str = "running",
    container_id: str = "abc123",
    container_name: str = "test-container",
) -> SessionDTO:
    """Build a SessionDTO with the given status."""
    return SessionDTO(
        id=SESSION_ID,
        user_id=USER_ID,
        container_id=container_id,
        container_name=container_name,
        status=SessionStatus(status),
        agent_type="claude-code",
        system_prompt=None,
        last_activity_at=_now() - timedelta(minutes=1),
        metadata=None,
        created_at=_now() - timedelta(minutes=5),
    )


def _make_update(telegram_id: int = TELEGRAM_ID) -> MagicMock:
    """Build a mock Telegram Update carrying a plain text message."""
    # The status message returned by reply_text must itself support edit_text.
    status_message = MagicMock()
    status_message.edit_text = AsyncMock()

    update = MagicMock()
    update.effective_user.id = telegram_id
    update.effective_chat.id = telegram_id
    update.message = MagicMock()
    update.message.text = "Hello"
    update.message.message_id = 42
    # reply_text is async and returns the status_message mock.
    update.message.reply_text = AsyncMock(return_value=status_message)

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
# Tests: new_command — error session guard
# ---------------------------------------------------------------------------


class TestNewCommandErrorSession:
    """Verify that new_command refuses to cache a session with status='error'."""

    @pytest.mark.asyncio
    async def test_error_session_not_cached(self):
        """When create_session returns status='error', bot_data must NOT store the session ID.

        A broken session must never land in the cache — if it did, every
        subsequent message would be routed to a dead container.
        """
        # Arrange: approved user, create_session returns an error session.
        approved_user = _make_user_dto(is_approved=True)
        error_session = _make_session_dto(status="error")

        api_client = AsyncMock()
        api_client.get_user = AsyncMock(return_value=approved_user)
        api_client.create_session = AsyncMock(return_value=error_session)

        update = _make_update()
        context = _make_context(api_client)

        # Act: run the command.
        await new_command(update, context)

        # Assert: no session key was written to bot_data.
        session_cache_key = f"session:{TELEGRAM_ID}"
        assert session_cache_key not in context.bot_data, (
            "An error session must never be written to the bot_data cache."
        )

    @pytest.mark.asyncio
    async def test_error_session_shows_error_message(self):
        """When create_session returns status='error', the user sees a recovery hint."""
        # Arrange
        approved_user = _make_user_dto(is_approved=True)
        error_session = _make_session_dto(status="error")

        api_client = AsyncMock()
        api_client.get_user = AsyncMock(return_value=approved_user)
        api_client.create_session = AsyncMock(return_value=error_session)

        update = _make_update()
        context = _make_context(api_client)

        # Act
        await new_command(update, context)

        # Assert: the status message was edited with a failure text.
        status_message = update.message.reply_text.return_value
        status_message.edit_text.assert_awaited_once()

        edited_text = status_message.edit_text.call_args[0][0]
        assert "failed to start" in edited_text.lower(), (
            "Error message should tell the user the container failed to start."
        )
        assert "/new" in edited_text, (
            "Error message should suggest /new so the user knows how to recover."
        )

    @pytest.mark.asyncio
    async def test_healthy_session_is_cached(self):
        """When create_session returns status='running', the session ID is stored in bot_data."""
        # Arrange
        approved_user = _make_user_dto(is_approved=True)
        running_session = _make_session_dto(status="running")

        api_client = AsyncMock()
        api_client.get_user = AsyncMock(return_value=approved_user)
        api_client.create_session = AsyncMock(return_value=running_session)

        update = _make_update()
        context = _make_context(api_client)

        # Act
        await new_command(update, context)

        # Assert: the session ID was written to bot_data.
        session_cache_key = f"session:{TELEGRAM_ID}"
        assert session_cache_key in context.bot_data, (
            "A healthy session must be stored in bot_data so message handlers can find it."
        )
        cached_value = context.bot_data[session_cache_key]
        assert cached_value == str(SESSION_ID), (
            "Cached session ID must match the session returned by create_session."
        )

    @pytest.mark.asyncio
    async def test_healthy_session_shows_success(self):
        """When create_session returns status='running', the user sees 'Container ready'."""
        # Arrange
        approved_user = _make_user_dto(is_approved=True)
        running_session = _make_session_dto(status="running", container_name="my-agent")

        api_client = AsyncMock()
        api_client.get_user = AsyncMock(return_value=approved_user)
        api_client.create_session = AsyncMock(return_value=running_session)

        update = _make_update()
        context = _make_context(api_client)

        # Act
        await new_command(update, context)

        # Assert: the status message was edited with a success text.
        status_message = update.message.reply_text.return_value
        status_message.edit_text.assert_awaited_once()

        # The success message is MarkdownV2 formatted; check for the literal keyword.
        edited_text = status_message.edit_text.call_args[0][0]
        assert "Container ready" in edited_text, (
            "Success message should tell the user the container is ready."
        )


# ---------------------------------------------------------------------------
# Tests: default_message_handler — cache invalidation on exception
# ---------------------------------------------------------------------------


class TestMessageHandlerCacheInvalidation:
    """Verify that default_message_handler clears the session cache on error.

    If streaming fails (e.g. container is dead), keeping the stale session ID
    in bot_data would cause every future message to fail in the same way.
    Clearing it lets the user run /new to get a fresh container.
    """

    @pytest.mark.asyncio
    async def test_exception_clears_cached_session(self):
        """When stream_message_events raises, the session ID is removed from bot_data."""
        # Arrange: approved user, cached session, streaming fails.
        approved_user = _make_user_dto(is_approved=True)

        # Pre-populate the cache as if the user already has a container.
        session_cache_key = f"session:{TELEGRAM_ID}"
        cached_session_id = str(SESSION_ID)

        api_client = AsyncMock()
        api_client.get_user = AsyncMock(return_value=approved_user)

        # stream_message_events must be an async generator that raises immediately.
        async def _failing_stream(*args, **kwargs):
            raise RuntimeError("Container is unreachable")
            # This yield is never reached but makes the function an async generator.
            yield {}  # noqa: unreachable

        api_client.stream_message_events = _failing_stream

        update = _make_update()
        context = _make_context(
            api_client,
            extra_bot_data={session_cache_key: cached_session_id},
        )

        # Patch the renderer so it doesn't attempt real Telegram calls.
        with patch(
            "telegram_bot.handlers.message.TelegramStreamRenderer",
            autospec=True,
        ) as MockRenderer:
            renderer_instance = MockRenderer.return_value
            renderer_instance.start = AsyncMock()
            renderer_instance.handle_event = AsyncMock()
            renderer_instance.finalize = AsyncMock()

            # Act
            await default_message_handler(update, context)

        # Assert: the cache entry was removed so the user can create a new session.
        assert session_cache_key not in context.bot_data, (
            "A failed stream must clear the cached session so the user is not stuck."
        )

    @pytest.mark.asyncio
    async def test_error_message_suggests_new(self):
        """When streaming fails and renderer also fails, a fallback message is sent.

        The fallback message must suggest /new (not /restart) because the
        container may be completely gone and /restart would also fail.
        """
        # Arrange
        approved_user = _make_user_dto(is_approved=True)
        session_cache_key = f"session:{TELEGRAM_ID}"
        cached_session_id = str(SESSION_ID)

        api_client = AsyncMock()
        api_client.get_user = AsyncMock(return_value=approved_user)

        # Streaming raises immediately.
        async def _failing_stream(*args, **kwargs):
            raise RuntimeError("Connection refused")
            yield {}  # noqa: unreachable

        api_client.stream_message_events = _failing_stream

        update = _make_update()
        context = _make_context(
            api_client,
            extra_bot_data={session_cache_key: cached_session_id},
        )

        # Patch renderer so that handle_event (the error recovery path) also fails,
        # forcing the handler to fall back to update.message.reply_text.
        with patch(
            "telegram_bot.handlers.message.TelegramStreamRenderer",
            autospec=True,
        ) as MockRenderer:
            renderer_instance = MockRenderer.return_value
            renderer_instance.start = AsyncMock()
            renderer_instance.handle_event = AsyncMock(
                side_effect=Exception("renderer also failed")
            )
            renderer_instance.finalize = AsyncMock()

            # Act
            await default_message_handler(update, context)

        # Assert: the fallback reply_text was called and suggests /new.
        update.message.reply_text.assert_awaited_once()
        fallback_text = update.message.reply_text.call_args[0][0]
        assert "/new" in fallback_text, (
            "The fallback error message must suggest /new so the user can recover."
        )
        assert "restart" not in fallback_text.lower(), (
            "The fallback must NOT suggest /restart — the container may be gone."
        )
