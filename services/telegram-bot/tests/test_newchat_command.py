"""Tests for the /newchat command handler.

Verifies:
1. Unapproved users are rejected before any API call is made.
2. A user with no active session is told to run /new first.
3. A user with an active session gets their conversation cleared.
4. An API error is surfaced to the user without crashing the handler.
"""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from telegram_bot.commands.session import newchat_command

from chatops_shared.schemas.user import UserDTO, UserRole

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
    return datetime.now(UTC)


def _make_user_dto(is_approved: bool = True) -> UserDTO:
    """Build a minimal UserDTO for the test Telegram user."""
    return UserDTO(
        id=USER_ID,
        telegram_id=TELEGRAM_ID,
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


def _make_update(telegram_id: int = TELEGRAM_ID) -> MagicMock:
    """Build a mock Telegram Update with an async reply_text that returns a status message."""
    # The status message object returned by reply_text must support edit_text.
    status_message = MagicMock()
    status_message.edit_text = AsyncMock()

    update = MagicMock()
    update.effective_user.id = telegram_id
    update.effective_chat.id = telegram_id
    update.message = MagicMock()
    update.message.reply_text = AsyncMock(return_value=status_message)

    return update


def _make_context(
    api_client: MagicMock,
    extra_bot_data: dict | None = None,
) -> MagicMock:
    """Build a mock PTB context with the given api_client in bot_data.

    Pass extra_bot_data to pre-populate the session cache, e.g.:
        extra_bot_data={f"session:{TELEGRAM_ID}": str(SESSION_ID)}
    """
    bot_data: dict = {"api_client": api_client}
    if extra_bot_data:
        bot_data.update(extra_bot_data)

    context = MagicMock()
    context.bot_data = bot_data
    context.args = []
    context.bot = MagicMock()
    return context


# ---------------------------------------------------------------------------
# Tests: TestNewchatCommand
# ---------------------------------------------------------------------------


class TestNewchatCommand:
    """Tests for newchat_command — the /newchat Telegram handler."""

    @pytest.mark.asyncio
    async def test_unapproved_user_rejected(self):
        """An unapproved user sees a 'pending approval' message and no API call is made.

        _require_approved returns False for unapproved users, so the handler
        must return early without ever touching new_conversation.
        """
        # Arrange: the user exists but has not yet been approved.
        unapproved_user = _make_user_dto(is_approved=False)

        api_client = AsyncMock()
        api_client.get_user = AsyncMock(return_value=unapproved_user)
        api_client.new_conversation = AsyncMock()

        update = _make_update()
        context = _make_context(api_client)

        # Act
        await newchat_command(update, context)

        # Assert: the user was told about the approval gate.
        reply_text_call = update.message.reply_text.call_args[0][0]
        assert "pending" in reply_text_call.lower(), (
            "The reply should mention 'pending' so the user understands why they were blocked."
        )
        assert "approval" in reply_text_call.lower(), (
            "The reply should mention 'approval' to clarify who can unblock them."
        )

        # Assert: the actual API endpoint was never reached.
        api_client.new_conversation.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_no_session_shows_hint(self):
        """An approved user with no active session is told to use /new.

        When _get_session_id finds nothing in bot_data and the API also returns
        None, the handler shows a 'No active session' message and exits early.
        """
        # Arrange: approved user, nothing in the session cache, API returns None.
        approved_user = _make_user_dto(is_approved=True)

        api_client = AsyncMock()
        api_client.get_user = AsyncMock(return_value=approved_user)
        api_client.get_active_session_by_telegram_id = AsyncMock(return_value=None)
        api_client.new_conversation = AsyncMock()

        update = _make_update()
        # No extra_bot_data — the session cache is empty.
        context = _make_context(api_client)

        # Act
        await newchat_command(update, context)

        # Assert: user received the 'no session' hint.
        reply_text_call = update.message.reply_text.call_args[0][0]
        assert "No active session" in reply_text_call, (
            "When there is no session, the reply must say 'No active session'."
        )

        # Assert: no attempt was made to clear the conversation.
        api_client.new_conversation.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_success_clears_conversation(self):
        """A happy-path call clears the conversation and confirms it to the user.

        When the user has an active session and new_conversation succeeds,
        the status message is edited to confirm the context was cleared.
        """
        # Arrange: approved user with a pre-cached session ID.
        approved_user = _make_user_dto(is_approved=True)
        session_cache_key = f"session:{TELEGRAM_ID}"

        api_client = AsyncMock()
        api_client.get_user = AsyncMock(return_value=approved_user)
        api_client.new_conversation = AsyncMock(return_value=None)

        update = _make_update()
        context = _make_context(
            api_client,
            extra_bot_data={session_cache_key: str(SESSION_ID)},
        )

        # Act
        await newchat_command(update, context)

        # Assert: new_conversation was called with the correct UUID.
        api_client.new_conversation.assert_awaited_once_with(SESSION_ID)

        # Assert: the status message was updated with a success confirmation.
        status_message = update.message.reply_text.return_value
        status_message.edit_text.assert_awaited_once()

        edited_text = status_message.edit_text.call_args[0][0]
        assert "New conversation started" in edited_text, (
            "The success edit must tell the user that a new conversation was started."
        )
        assert "cleared" in edited_text, (
            "The success edit must confirm that the previous context was cleared."
        )

    @pytest.mark.asyncio
    async def test_api_error_shows_failure(self):
        """When new_conversation raises, the user sees the failure message.

        The handler must catch the exception and edit the status message to
        show an error instead of crashing with an unhandled exception.
        """
        # Arrange: approved user with a cached session, but the API call fails.
        approved_user = _make_user_dto(is_approved=True)
        session_cache_key = f"session:{TELEGRAM_ID}"
        api_error = RuntimeError("connection failed")

        api_client = AsyncMock()
        api_client.get_user = AsyncMock(return_value=approved_user)
        api_client.new_conversation = AsyncMock(side_effect=api_error)

        update = _make_update()
        context = _make_context(
            api_client,
            extra_bot_data={session_cache_key: str(SESSION_ID)},
        )

        # Act — must not raise even though new_conversation raises.
        await newchat_command(update, context)

        # Assert: the status message was edited with an error description.
        status_message = update.message.reply_text.return_value
        status_message.edit_text.assert_awaited_once()

        edited_text = status_message.edit_text.call_args[0][0]
        assert "Failed to start new conversation" in edited_text, (
            "The error edit must say 'Failed to start new conversation'."
        )
        assert "connection failed" in edited_text, (
            "The error edit must include the original exception message."
        )
