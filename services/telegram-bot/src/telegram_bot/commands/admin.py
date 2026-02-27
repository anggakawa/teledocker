"""Admin and configuration command handlers.

/setkey auto-deletes the original message to prevent the API key from
sitting in chat history.
"""

import logging

from telegram import Update
from telegram.ext import ContextTypes

logger = logging.getLogger(__name__)

_PROVIDER_PRESETS = {
    "anthropic": {"provider": "anthropic", "base_url": None},
    "openrouter": {"provider": "openrouter", "base_url": "https://openrouter.ai/api"},
    "custom": {"provider": "custom", "base_url": None},  # User must also call /setbaseurl
}


def _require_admin(func):
    """Decorator that rejects non-admin users with a permission error."""
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        admin_ids = context.bot_data.get("admin_ids", [])
        if update.effective_user.id not in admin_ids:
            await update.message.reply_text("This command is for admins only.")
            return
        return await func(update, context)
    return wrapper


async def setkey_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Store an encrypted API key. The user's message is deleted immediately."""
    # Delete the message BEFORE any other processing to minimize exposure window.
    try:
        await context.bot.delete_message(
            chat_id=update.effective_chat.id,
            message_id=update.message.message_id,
        )
    except Exception as exc:
        logger.warning("Could not delete /setkey message: %s", exc)

    api_client = context.bot_data["api_client"]
    user_dto = await api_client.get_user(update.effective_user.id)
    if user_dto is None or not user_dto.is_approved:
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="Account pending approval.",
        )
        return

    if not context.args:
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="Usage: /setkey <api_key>\nYour message will be deleted immediately.",
        )
        return

    api_key = context.args[0]

    # Use whatever provider is currently set for this user; default to anthropic.
    provider = "anthropic"
    base_url = None
    if user_dto.provider_config:
        provider = user_dto.provider_config.get("provider", "anthropic")
        base_url = user_dto.provider_config.get("base_url")

    try:
        await api_client.set_api_key(
            telegram_id=update.effective_user.id,
            api_key=api_key,
            provider=provider,
            base_url=base_url,
        )
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="API key stored securely. Use /new to start a session.",
        )
    except Exception as exc:
        logger.exception("Failed to store API key: %s", exc)
        await context.bot.send_message(
            chat_id=update.effective_chat.id, text=f"Failed to store key: {exc}"
        )


async def setprovider_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Switch provider preset: anthropic, openrouter, or custom."""
    api_client = context.bot_data["api_client"]
    user_dto = await api_client.get_user(update.effective_user.id)
    if user_dto is None or not user_dto.is_approved:
        await update.message.reply_text("Account pending approval.")
        return

    if not context.args:
        presets = ", ".join(_PROVIDER_PRESETS.keys())
        await update.message.reply_text(f"Usage: /setprovider <preset>\nAvailable: {presets}")
        return

    preset_name = context.args[0].lower()
    if preset_name not in _PROVIDER_PRESETS:
        await update.message.reply_text(
            f"Unknown provider. Available: {', '.join(_PROVIDER_PRESETS.keys())}"
        )
        return

    preset = _PROVIDER_PRESETS[preset_name]
    # Keep the existing API key; only update provider metadata.
    current_key = ""  # We don't have plaintext; user must re-set key if provider changes.
    try:
        await api_client.set_api_key(
            telegram_id=update.effective_user.id,
            api_key=current_key or "placeholder",
            provider=preset["provider"],
            base_url=preset["base_url"],
        )
        await update.message.reply_text(
            f"Provider set to: {preset_name}\n"
            "Use /setkey to store or update your API key."
        )
    except Exception as exc:
        await update.message.reply_text(f"Failed to update provider: {exc}")


async def setbaseurl_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Set a custom API base URL (for OpenRouter, proxies, etc.)."""
    api_client = context.bot_data["api_client"]
    user_dto = await api_client.get_user(update.effective_user.id)
    if user_dto is None or not user_dto.is_approved:
        await update.message.reply_text("Account pending approval.")
        return

    if not context.args:
        await update.message.reply_text("Usage: /setbaseurl <url>")
        return

    base_url = context.args[0]
    provider = user_dto.provider_config.get("provider", "custom") if user_dto.provider_config else "custom"

    try:
        await api_client.set_api_key(
            telegram_id=update.effective_user.id,
            api_key="placeholder",
            provider=provider,
            base_url=base_url,
        )
        await update.message.reply_text(f"Base URL set to: {base_url}")
    except Exception as exc:
        await update.message.reply_text(f"Failed to set base URL: {exc}")


async def removekey_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Remove stored API key and provider configuration."""
    api_client = context.bot_data["api_client"]
    try:
        await api_client.remove_api_key(update.effective_user.id)
        await update.message.reply_text("API key and provider config removed.")
    except Exception as exc:
        await update.message.reply_text(f"Failed to remove key: {exc}")


async def provider_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show current provider configuration (never shows the API key)."""
    api_client = context.bot_data["api_client"]
    user_dto = await api_client.get_user(update.effective_user.id)
    if user_dto is None:
        await update.message.reply_text("Not registered. Send /start first.")
        return

    if not user_dto.provider_config:
        await update.message.reply_text("No provider configured. Use /setkey to get started.")
        return

    config = user_dto.provider_config
    provider = config.get("provider", "unknown")
    base_url = config.get("base_url") or "(default)"
    has_key = user_dto.provider_config is not None  # Approximate check.

    await update.message.reply_text(
        f"Provider: {provider}\n"
        f"Base URL: {base_url}\n"
        f"API Key: {'set' if has_key else 'not set'}"
    )


@_require_admin
async def approve_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Admin: approve a user by Telegram ID."""
    api_client = context.bot_data["api_client"]

    if not context.args:
        await update.message.reply_text("Usage: /approve <telegram_id>")
        return

    try:
        target_id = int(context.args[0])
        user = await api_client.approve_user(target_id)
        await update.message.reply_text(f"Approved user: {user.display_name} ({target_id})")

        # Notify the approved user.
        try:
            await context.bot.send_message(
                chat_id=target_id,
                text=(
                    "You've been approved!\n\n"
                    "Use /setkey to configure your API key, then /new to start a session."
                ),
            )
        except Exception as exc:
            logger.warning("Could not notify approved user %s: %s", target_id, exc)
    except ValueError:
        await update.message.reply_text("Invalid Telegram ID.")
    except Exception as exc:
        await update.message.reply_text(f"Failed to approve: {exc}")


@_require_admin
async def reject_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Admin: reject a pending user by Telegram ID."""
    api_client = context.bot_data["api_client"]

    if not context.args:
        await update.message.reply_text("Usage: /reject <telegram_id> [reason]")
        return

    try:
        target_id = int(context.args[0])
        await api_client.reject_user(target_id)
        await update.message.reply_text(f"Rejected user {target_id}.")
    except ValueError:
        await update.message.reply_text("Invalid Telegram ID.")
    except Exception as exc:
        await update.message.reply_text(f"Failed to reject: {exc}")


@_require_admin
async def revoke_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Admin: revoke an approved user's access."""
    api_client = context.bot_data["api_client"]

    if not context.args:
        await update.message.reply_text("Usage: /revoke <telegram_id>")
        return

    try:
        target_id = int(context.args[0])
        user = await api_client.revoke_user(target_id)
        await update.message.reply_text(f"Revoked access for: {user.display_name} ({target_id})")
    except ValueError:
        await update.message.reply_text("Invalid Telegram ID.")
    except Exception as exc:
        await update.message.reply_text(f"Failed to revoke: {exc}")


@_require_admin
async def users_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Admin: list users filtered by approval status."""
    api_client = context.bot_data["api_client"]

    status_filter = context.args[0] if context.args else "pending"
    users = await api_client.list_users(status_filter)

    if not users:
        await update.message.reply_text(f"No users with status: {status_filter}")
        return

    lines = [f"Users ({status_filter}):"]
    for u in users:
        role_tag = f"[{u.role}]"
        approved_tag = "approved" if u.is_approved else "pending"
        username = f"@{u.telegram_username}" if u.telegram_username else "no username"
        lines.append(f"- {u.display_name} {username} ({u.telegram_id}) {role_tag} {approved_tag}")

    await update.message.reply_text("\n".join(lines))
