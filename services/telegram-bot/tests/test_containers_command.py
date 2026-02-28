"""Tests for the admin /containers command and admin_destroy callback.

These tests mock the ApiClient and Telegram bot APIs to verify:
  - containers_command lists sessions with destroy buttons
  - admin_destroy callback destroys the session and confirms
  - Non-admin users are rejected
  - Edge cases: no sessions, already-destroyed sessions
"""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import httpx
import pytest
from telegram_bot.commands.admin import containers_command
from telegram_bot.handlers.callback import _handle_admin_destroy_session

from chatops_shared.schemas.session import SessionDTO, SessionStatus
from chatops_shared.schemas.user import UserDTO, UserRole

ADMIN_ID = 111111
NON_ADMIN_ID = 999999


def _now() -> datetime:
    return datetime.now(UTC)


def _make_session_dto(
    status: str = "running",
    user_id=None,
    container_name: str = "agent-test",
) -> SessionDTO:
    return SessionDTO(
        id=uuid4(),
        user_id=user_id or uuid4(),
        container_id=f"ctr-{uuid4().hex[:8]}",
        container_name=container_name,
        status=SessionStatus(status),
        agent_type="claude-code",
        system_prompt=None,
        last_activity_at=_now() - timedelta(minutes=10),
        metadata=None,
        created_at=_now() - timedelta(hours=1),
    )


def _make_user_dto(user_id=None, telegram_id: int = 12345) -> UserDTO:
    return UserDTO(
        id=user_id or uuid4(),
        telegram_id=telegram_id,
        telegram_username="testuser",
        display_name="Test User",
        role=UserRole.user,
        is_approved=True,
        is_active=True,
        max_containers=5,
        provider_config=None,
        created_at=_now(),
        updated_at=_now(),
    )


def _make_update_and_context(
    user_id: int = ADMIN_ID,
    admin_ids: list[int] | None = None,
):
    """Build mocked Update and Context objects for command tests."""
    update = MagicMock()
    update.effective_user.id = user_id
    update.message = AsyncMock()
    update.message.reply_text = AsyncMock()

    context = MagicMock()
    context.bot_data = {
        "api_client": AsyncMock(),
        "admin_ids": admin_ids if admin_ids is not None else [ADMIN_ID],
    }
    context.args = []

    return update, context


def _make_query_and_context(
    user_id: int = ADMIN_ID,
    callback_data: str = "",
    admin_ids: list[int] | None = None,
):
    """Build mocked CallbackQuery and Context for callback tests."""
    query = MagicMock()
    query.from_user.id = user_id
    query.data = callback_data
    query.answer = AsyncMock()
    query.edit_message_text = AsyncMock()

    context = MagicMock()
    context.bot_data = {
        "api_client": AsyncMock(),
        "admin_ids": admin_ids if admin_ids is not None else [ADMIN_ID],
    }

    return query, context


# ---------------------------------------------------------------------------
# Tests: containers_command
# ---------------------------------------------------------------------------


class TestContainersCommand:
    """Verify the admin /containers command handler."""

    @pytest.mark.asyncio
    async def test_non_admin_is_rejected(self):
        """A non-admin user should get a permission error."""
        update, context = _make_update_and_context(user_id=NON_ADMIN_ID)

        await containers_command(update, context)

        update.message.reply_text.assert_awaited_once_with(
            "This command is for admins only."
        )

    @pytest.mark.asyncio
    async def test_no_sessions_found(self):
        """When there are no sessions, reply with 'No sessions found.'"""
        update, context = _make_update_and_context()
        context.bot_data["api_client"].list_sessions = AsyncMock(return_value=[])

        await containers_command(update, context)

        update.message.reply_text.assert_awaited_once_with("No sessions found.")

    @pytest.mark.asyncio
    async def test_lists_sessions_with_keyboard(self):
        """When sessions exist, reply should contain session info and a keyboard."""
        user_id = uuid4()
        session = _make_session_dto(user_id=user_id)
        user = _make_user_dto(user_id=user_id)

        update, context = _make_update_and_context()
        api_client = context.bot_data["api_client"]
        api_client.list_sessions = AsyncMock(return_value=[session])
        api_client.list_users = AsyncMock(return_value=[user])

        await containers_command(update, context)

        call_args = update.message.reply_text.call_args
        text = call_args[0][0]
        assert "Containers (1):" in text
        assert "Test User" in text
        # Should have a reply_markup keyword arg (the keyboard).
        assert call_args[1].get("reply_markup") is not None

    @pytest.mark.asyncio
    async def test_sessions_without_matching_users(self):
        """Sessions with no matching user should show user_id fallback."""
        session = _make_session_dto()

        update, context = _make_update_and_context()
        api_client = context.bot_data["api_client"]
        api_client.list_sessions = AsyncMock(return_value=[session])
        api_client.list_users = AsyncMock(return_value=[])

        await containers_command(update, context)

        text = update.message.reply_text.call_args[0][0]
        assert f"user_id={session.user_id}" in text


# ---------------------------------------------------------------------------
# Tests: admin_destroy callback
# ---------------------------------------------------------------------------


class TestAdminDestroyCallback:
    """Verify the admin_destroy inline button callback handler."""

    @pytest.mark.asyncio
    async def test_non_admin_is_rejected(self):
        """A non-admin pressing destroy should be rejected."""
        session_id = str(uuid4())
        query, context = _make_query_and_context(user_id=NON_ADMIN_ID)

        await _handle_admin_destroy_session(query, context, session_id)

        query.edit_message_text.assert_awaited_once_with(
            "Only admins can destroy sessions."
        )

    @pytest.mark.asyncio
    async def test_successful_destroy(self):
        """Admin destroying a session should call destroy_session and confirm."""
        session_id = str(uuid4())
        query, context = _make_query_and_context()
        context.bot_data["api_client"].destroy_session = AsyncMock()

        await _handle_admin_destroy_session(query, context, session_id)

        context.bot_data["api_client"].destroy_session.assert_awaited_once()
        call_text = query.edit_message_text.call_args[0][0]
        assert "destroyed" in call_text.lower()

    @pytest.mark.asyncio
    async def test_already_destroyed_shows_message(self):
        """A 404 (session already gone) should show a friendly message."""
        session_id = str(uuid4())
        query, context = _make_query_and_context()

        mock_response = MagicMock()
        mock_response.status_code = 404
        error = httpx.HTTPStatusError(
            "Not Found", request=MagicMock(), response=mock_response
        )
        context.bot_data["api_client"].destroy_session = AsyncMock(side_effect=error)

        await _handle_admin_destroy_session(query, context, session_id)

        query.edit_message_text.assert_awaited_once_with("Session already destroyed.")

    @pytest.mark.asyncio
    async def test_other_http_error_shows_failure(self):
        """A non-404 HTTP error should show a failure message."""
        session_id = str(uuid4())
        query, context = _make_query_and_context()

        mock_response = MagicMock()
        mock_response.status_code = 500
        error = httpx.HTTPStatusError(
            "Server Error", request=MagicMock(), response=mock_response
        )
        context.bot_data["api_client"].destroy_session = AsyncMock(side_effect=error)

        await _handle_admin_destroy_session(query, context, session_id)

        call_text = query.edit_message_text.call_args[0][0]
        assert "Destroy failed" in call_text
