"""Tests for /setmodel command handler.

Verifies:
1. Preset model names (opus, sonnet, haiku) are mapped correctly.
2. Custom model IDs are passed through verbatim.
3. No-args shows usage help.
4. Unapproved users are rejected.
5. Existing provider and base_url are preserved when setting model.
"""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from telegram_bot.commands.admin import setmodel_command

from chatops_shared.schemas.user import UserDTO, UserRole

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

TELEGRAM_ID = 99999
USER_ID = uuid4()


def _now() -> datetime:
    return datetime.now(UTC)


def _make_user_dto(
    is_approved: bool = True,
    provider_config: dict | None = None,
) -> UserDTO:
    return UserDTO(
        id=USER_ID,
        telegram_id=TELEGRAM_ID,
        telegram_username="testuser",
        display_name="Test User",
        role=UserRole.user,
        is_approved=is_approved,
        is_active=True,
        max_containers=5,
        provider_config=provider_config,
        created_at=_now(),
        updated_at=_now(),
    )


def _make_update(telegram_id: int = TELEGRAM_ID) -> MagicMock:
    update = MagicMock()
    update.effective_user.id = telegram_id
    update.effective_chat.id = telegram_id
    update.message = MagicMock()
    update.message.reply_text = AsyncMock()
    return update


def _make_context(api_client: MagicMock, args: list[str] | None = None) -> MagicMock:
    context = MagicMock()
    context.bot_data = {"api_client": api_client}
    context.args = args or []
    context.bot = MagicMock()
    return context


# ---------------------------------------------------------------------------
# Tests: preset model names
# ---------------------------------------------------------------------------


class TestSetmodelPresets:
    """Preset names (opus, sonnet, haiku) should map to SDK-friendly aliases."""

    @pytest.mark.asyncio
    async def test_opus_preset(self):
        """'/setmodel opus' should pass model='opus' to the API."""
        user = _make_user_dto(provider_config={"provider": "anthropic", "base_url": None})
        api_client = AsyncMock()
        api_client.get_user = AsyncMock(return_value=user)
        api_client.update_provider = AsyncMock()

        update = _make_update()
        context = _make_context(api_client, args=["opus"])

        await setmodel_command(update, context)

        api_client.update_provider.assert_awaited_once_with(
            telegram_id=TELEGRAM_ID,
            provider="anthropic",
            base_url=None,
            model="opus",
        )
        reply_text = update.message.reply_text.call_args[0][0]
        assert "opus" in reply_text

    @pytest.mark.asyncio
    async def test_sonnet_preset(self):
        """'/setmodel sonnet' should pass model='sonnet'."""
        user = _make_user_dto(provider_config={"provider": "anthropic"})
        api_client = AsyncMock()
        api_client.get_user = AsyncMock(return_value=user)
        api_client.update_provider = AsyncMock()

        update = _make_update()
        context = _make_context(api_client, args=["sonnet"])

        await setmodel_command(update, context)

        call_kwargs = api_client.update_provider.call_args.kwargs
        assert call_kwargs["model"] == "sonnet"

    @pytest.mark.asyncio
    async def test_haiku_preset(self):
        """'/setmodel haiku' should pass model='haiku'."""
        user = _make_user_dto(provider_config={"provider": "anthropic"})
        api_client = AsyncMock()
        api_client.get_user = AsyncMock(return_value=user)
        api_client.update_provider = AsyncMock()

        update = _make_update()
        context = _make_context(api_client, args=["haiku"])

        await setmodel_command(update, context)

        call_kwargs = api_client.update_provider.call_args.kwargs
        assert call_kwargs["model"] == "haiku"

    @pytest.mark.asyncio
    async def test_preset_case_insensitive(self):
        """'/setmodel OPUS' (uppercase) should resolve to 'opus'."""
        user = _make_user_dto(provider_config={"provider": "anthropic"})
        api_client = AsyncMock()
        api_client.get_user = AsyncMock(return_value=user)
        api_client.update_provider = AsyncMock()

        update = _make_update()
        context = _make_context(api_client, args=["OPUS"])

        await setmodel_command(update, context)

        call_kwargs = api_client.update_provider.call_args.kwargs
        assert call_kwargs["model"] == "opus"


# ---------------------------------------------------------------------------
# Tests: custom model ID
# ---------------------------------------------------------------------------


class TestSetmodelCustomId:
    """Non-preset model IDs should be passed through verbatim."""

    @pytest.mark.asyncio
    async def test_custom_model_id(self):
        """'/setmodel claude-sonnet-4-5-20250514' passes the full ID."""
        custom_id = "claude-sonnet-4-5-20250514"
        user = _make_user_dto(provider_config={"provider": "anthropic"})
        api_client = AsyncMock()
        api_client.get_user = AsyncMock(return_value=user)
        api_client.update_provider = AsyncMock()

        update = _make_update()
        context = _make_context(api_client, args=[custom_id])

        await setmodel_command(update, context)

        call_kwargs = api_client.update_provider.call_args.kwargs
        assert call_kwargs["model"] == custom_id


# ---------------------------------------------------------------------------
# Tests: no arguments
# ---------------------------------------------------------------------------


class TestSetmodelNoArgs:
    """'/setmodel' with no arguments should show usage help."""

    @pytest.mark.asyncio
    async def test_shows_usage(self):
        user = _make_user_dto()
        api_client = AsyncMock()
        api_client.get_user = AsyncMock(return_value=user)

        update = _make_update()
        context = _make_context(api_client, args=[])

        await setmodel_command(update, context)

        reply_text = update.message.reply_text.call_args[0][0]
        assert "Usage" in reply_text
        assert "opus" in reply_text
        api_client.update_provider.assert_not_awaited()


# ---------------------------------------------------------------------------
# Tests: unapproved user
# ---------------------------------------------------------------------------


class TestSetmodelUnapproved:
    """Unapproved users should be rejected."""

    @pytest.mark.asyncio
    async def test_unapproved_user_rejected(self):
        user = _make_user_dto(is_approved=False)
        api_client = AsyncMock()
        api_client.get_user = AsyncMock(return_value=user)

        update = _make_update()
        context = _make_context(api_client, args=["opus"])

        await setmodel_command(update, context)

        reply_text = update.message.reply_text.call_args[0][0]
        assert "pending" in reply_text.lower()
        api_client.update_provider.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_unknown_user_rejected(self):
        api_client = AsyncMock()
        api_client.get_user = AsyncMock(return_value=None)

        update = _make_update()
        context = _make_context(api_client, args=["opus"])

        await setmodel_command(update, context)

        reply_text = update.message.reply_text.call_args[0][0]
        assert "pending" in reply_text.lower()


# ---------------------------------------------------------------------------
# Tests: preserves existing provider config
# ---------------------------------------------------------------------------


class TestSetmodelPreservesConfig:
    """Setting the model should preserve the existing provider and base_url."""

    @pytest.mark.asyncio
    async def test_preserves_openrouter_provider(self):
        """When user is on openrouter, /setmodel should keep provider=openrouter."""
        user = _make_user_dto(
            provider_config={
                "provider": "openrouter",
                "base_url": "https://openrouter.ai/api",
                "model": "old-model",
            }
        )
        api_client = AsyncMock()
        api_client.get_user = AsyncMock(return_value=user)
        api_client.update_provider = AsyncMock()

        update = _make_update()
        context = _make_context(api_client, args=["opus"])

        await setmodel_command(update, context)

        api_client.update_provider.assert_awaited_once_with(
            telegram_id=TELEGRAM_ID,
            provider="openrouter",
            base_url="https://openrouter.ai/api",
            model="opus",
        )

    @pytest.mark.asyncio
    async def test_no_existing_config_defaults_to_anthropic(self):
        """When provider_config is None, should default to anthropic."""
        user = _make_user_dto(provider_config=None)
        api_client = AsyncMock()
        api_client.get_user = AsyncMock(return_value=user)
        api_client.update_provider = AsyncMock()

        update = _make_update()
        context = _make_context(api_client, args=["sonnet"])

        await setmodel_command(update, context)

        call_kwargs = api_client.update_provider.call_args.kwargs
        assert call_kwargs["provider"] == "anthropic"
        assert call_kwargs["model"] == "sonnet"
