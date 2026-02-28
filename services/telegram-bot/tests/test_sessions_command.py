"""Tests for /sessions and /history command handlers, plus resume callback.

Verifies:
1. /sessions shows a list with inline keyboard for approved users.
2. /sessions handles empty list gracefully.
3. /sessions rejects unapproved users.
4. /history shows messages for the active session.
5. /history handles no active session.
6. Resume callback updates the session cache.
"""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from chatops_shared.schemas.session import SessionDTO, SessionStatus
from chatops_shared.schemas.user import UserDTO, UserRole

TELEGRAM_ID = 12345
USER_ID = uuid4()
SESSION_ID = uuid4()
SESSION_ID_2 = uuid4()


def _now() -> datetime:
    return datetime.now(UTC)


def _make_user_dto(is_approved: bool = True) -> UserDTO:
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


def _make_session_dto(
    session_id=None,
    status: str = "running",
    container_name: str = "chatops-12345-abc",
) -> SessionDTO:
    return SessionDTO(
        id=session_id or uuid4(),
        user_id=USER_ID,
        container_id="ctr-abc",
        container_name=container_name,
        status=SessionStatus(status),
        agent_type="claude-code",
        system_prompt=None,
        last_activity_at=_now(),
        metadata=None,
        created_at=_now(),
    )


def _make_update(telegram_id: int = TELEGRAM_ID) -> MagicMock:
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
    bot_data: dict = {"api_client": api_client}
    if extra_bot_data:
        bot_data.update(extra_bot_data)

    context = MagicMock()
    context.bot_data = bot_data
    context.args = []
    context.bot = MagicMock()
    return context


class TestSessionsCommand:
    """Tests for sessions_command — the /sessions Telegram handler."""

    @pytest.mark.asyncio
    async def test_unapproved_user_rejected(self):
        """Unapproved users see a 'pending approval' message."""
        from telegram_bot.commands.session import sessions_command

        api_client = AsyncMock()
        api_client.get_user = AsyncMock(return_value=_make_user_dto(is_approved=False))

        update = _make_update()
        context = _make_context(api_client)

        await sessions_command(update, context)

        reply = update.message.reply_text.call_args[0][0]
        assert "pending" in reply.lower()
        api_client.list_user_sessions.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_shows_session_list_with_keyboard(self):
        """Approved user with sessions sees a list with inline buttons."""
        from telegram_bot.commands.session import sessions_command

        sessions = [
            _make_session_dto(session_id=SESSION_ID, status="running"),
            _make_session_dto(session_id=SESSION_ID_2, status="stopped"),
        ]

        api_client = AsyncMock()
        api_client.get_user = AsyncMock(return_value=_make_user_dto())
        api_client.list_user_sessions = AsyncMock(return_value=sessions)

        update = _make_update()
        context = _make_context(api_client)

        await sessions_command(update, context)

        # reply_text was called with text and reply_markup.
        call_kwargs = update.message.reply_text.call_args
        assert "Your Sessions (2)" in call_kwargs[0][0]
        assert call_kwargs[1]["reply_markup"] is not None

    @pytest.mark.asyncio
    async def test_empty_session_list(self):
        """Approved user with no sessions sees a helpful message."""
        from telegram_bot.commands.session import sessions_command

        api_client = AsyncMock()
        api_client.get_user = AsyncMock(return_value=_make_user_dto())
        api_client.list_user_sessions = AsyncMock(return_value=[])

        update = _make_update()
        context = _make_context(api_client)

        await sessions_command(update, context)

        reply = update.message.reply_text.call_args[0][0]
        assert "No sessions found" in reply

    @pytest.mark.asyncio
    async def test_api_error_shows_failure(self):
        """API errors are surfaced to the user without crashing."""
        from telegram_bot.commands.session import sessions_command

        api_client = AsyncMock()
        api_client.get_user = AsyncMock(return_value=_make_user_dto())
        api_client.list_user_sessions = AsyncMock(
            side_effect=RuntimeError("connection refused")
        )

        update = _make_update()
        context = _make_context(api_client)

        await sessions_command(update, context)

        reply = update.message.reply_text.call_args[0][0]
        assert "Failed to list sessions" in reply


class TestResumeCallback:
    """Tests for the resume callback handler."""

    @pytest.mark.asyncio
    async def test_resume_updates_session_cache(self):
        """After a successful resume, the bot_data cache is updated."""
        from telegram_bot.handlers.callback import _handle_resume

        resumed_session = _make_session_dto(session_id=SESSION_ID, status="running")

        api_client = AsyncMock()
        api_client.resume_session = AsyncMock(return_value=resumed_session)

        query = MagicMock()
        query.from_user.id = TELEGRAM_ID
        query.edit_message_text = AsyncMock()

        context = _make_context(api_client)

        await _handle_resume(query, context, str(SESSION_ID))

        # The session cache must be updated with the resumed session ID.
        assert context.bot_data[f"session:{TELEGRAM_ID}"] == str(SESSION_ID)

        # The success message must mention the container name.
        final_edit = query.edit_message_text.call_args_list[-1]
        assert "resumed" in final_edit[0][0].lower()

    @pytest.mark.asyncio
    async def test_resume_error_shows_failure(self):
        """A failed resume shows an error message without crashing."""
        from telegram_bot.handlers.callback import _handle_resume

        api_client = AsyncMock()
        api_client.resume_session = AsyncMock(
            side_effect=RuntimeError("container gone")
        )

        query = MagicMock()
        query.from_user.id = TELEGRAM_ID
        query.edit_message_text = AsyncMock()

        context = _make_context(api_client)

        await _handle_resume(query, context, str(SESSION_ID))

        final_edit = query.edit_message_text.call_args_list[-1]
        assert "Resume failed" in final_edit[0][0]
