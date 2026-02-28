"""Inline keyboard callback query handler.

Callback data format: "<action>:<payload>"
Examples:
  "approve:123456789"    -- admin approves user
  "reject:123456789"     -- admin rejects user
  "confirm_stop:<uuid>"  -- user confirms stop
  "confirm_destroy:<uuid>" -- user confirms destroy
  "restart:<uuid>"       -- restart container
  "action:cancel"        -- cancel dialog
"""

import logging
from uuid import UUID

import httpx
from telegram import Update
from telegram.ext import ContextTypes

from telegram_bot.commands.session import _get_session_id
from telegram_bot.formatters import (
    format_message_history,
    format_session_button_label,
    format_session_list_for_user,
)
from telegram_bot.keyboards import session_detail_keyboard, session_list_keyboard

logger = logging.getLogger(__name__)


async def callback_query_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Route callback query to the appropriate action handler."""
    query = update.callback_query
    await query.answer()  # Acknowledge the button press immediately.

    if not query.data:
        return

    action, _, payload = query.data.partition(":")

    if action == "approve":
        await _handle_approve(query, context, int(payload))
    elif action == "reject":
        await _handle_reject(query, context, int(payload))
    elif action == "confirm_stop":
        await _handle_confirm_stop(query, context, payload)
    elif action == "confirm_destroy":
        await _handle_confirm_destroy(query, context, payload)
    elif action == "destroy_recreate":
        await _handle_destroy_recreate(query, context, payload)
    elif action == "restart":
        await _handle_restart(query, context, payload)
    elif action == "sess_detail":
        await _handle_session_detail(query, context, payload)
    elif action == "resume":
        await _handle_resume(query, context, payload)
    elif action == "sess_hist":
        await _handle_session_history(query, context, payload)
    elif action == "admin_destroy":
        await _handle_admin_destroy_session(query, context, payload)
    elif action == "admin_destroy_status":
        await _handle_admin_destroy_by_status(query, context, payload)
    elif action == "action":
        await _handle_generic_action(query, context, payload)
    else:
        await query.edit_message_text(f"Unknown action: {action}")


async def _handle_approve(query, context, target_telegram_id: int) -> None:
    """Admin approves a pending user."""
    api_client = context.bot_data["api_client"]
    admin_ids = context.bot_data.get("admin_ids", [])

    if query.from_user.id not in admin_ids:
        await query.edit_message_text("Only admins can approve users.")
        return

    try:
        user = await api_client.approve_user(target_telegram_id)
        await query.edit_message_text(
            f"Approved: {user.display_name} ({target_telegram_id})"
        )
        await context.bot.send_message(
            chat_id=target_telegram_id,
            text=(
                "You've been approved!\n\n"
                "Use /setkey to configure your API key, then /new to start a session."
            ),
        )
    except Exception as exc:
        logger.exception("Failed to approve user %s: %s", target_telegram_id, exc)
        await query.edit_message_text(f"Approval failed: {exc}")


async def _handle_reject(query, context, target_telegram_id: int) -> None:
    """Admin rejects a pending user."""
    api_client = context.bot_data["api_client"]
    admin_ids = context.bot_data.get("admin_ids", [])

    if query.from_user.id not in admin_ids:
        await query.edit_message_text("Only admins can reject users.")
        return

    try:
        await api_client.reject_user(target_telegram_id)
        await query.edit_message_text(f"Rejected user: {target_telegram_id}")
    except Exception as exc:
        logger.exception("Failed to reject user %s: %s", target_telegram_id, exc)
        await query.edit_message_text(f"Rejection failed: {exc}")


async def _handle_confirm_stop(query, context, session_id_hint: str) -> None:
    """User confirms container stop."""
    api_client = context.bot_data["api_client"]
    telegram_id = query.from_user.id

    # Use the helper to resolve session ID (cache + API fallback).
    session_id = await _get_session_id(telegram_id, context)
    if session_id is None:
        session_id = session_id_hint

    try:
        await api_client.stop_session(UUID(session_id))
        await query.edit_message_text(
            "Container stopped. Your workspace is preserved.\n"
            "Use /restart to resume or /new to create a fresh session."
        )
    except Exception as exc:
        logger.exception("Failed to stop session: %s", exc)
        await query.edit_message_text(f"Stop failed: {exc}")


async def _handle_confirm_destroy(query, context, session_id_hint: str) -> None:
    """User confirms permanent container destruction."""
    api_client = context.bot_data["api_client"]
    telegram_id = query.from_user.id

    session_id = await _get_session_id(telegram_id, context)
    if session_id is None:
        session_id = session_id_hint

    try:
        await api_client.destroy_session(UUID(session_id))
        # Remove the cached session ID.
        context.bot_data.pop(f"session:{telegram_id}", None)
        await query.edit_message_text(
            "Container and workspace permanently destroyed.\n"
            "Use /new to create a fresh session."
        )
    except Exception as exc:
        logger.exception("Failed to destroy session: %s", exc)
        await query.edit_message_text(f"Destroy failed: {exc}")


async def _handle_destroy_recreate(query, context, session_id: str) -> None:
    """Destroy the current container then immediately create a new one."""
    await _handle_confirm_destroy(query, context, session_id)
    # After destroying, the user can send /new -- don't auto-create to keep UX clear.


async def _handle_restart(query, context, session_id_hint: str) -> None:
    """Restart a stopped or errored container."""
    api_client = context.bot_data["api_client"]
    telegram_id = query.from_user.id

    session_id = await _get_session_id(telegram_id, context)
    if session_id is None:
        session_id = session_id_hint

    try:
        await api_client.restart_session(UUID(session_id))
        await query.edit_message_text("Container restarted. Send a message to continue.")
    except Exception as exc:
        logger.exception("Failed to restart session: %s", exc)
        await query.edit_message_text(f"Restart failed: {exc}")


async def _handle_session_detail(query, context, session_id: str) -> None:
    """Show details and action buttons for a selected session."""
    api_client = context.bot_data["api_client"]

    try:
        session = await api_client.get_session(UUID(session_id))
        if session is None:
            await query.edit_message_text("Session no longer exists.")
            return

        status_text = session.status.value
        lines = [
            f"Session: {session.container_name}",
            f"Status: {status_text}",
            f"Agent: {session.agent_type}",
        ]
        await query.edit_message_text(
            "\n".join(lines),
            reply_markup=session_detail_keyboard(session_id, status_text),
        )
    except Exception as exc:
        logger.exception("Failed to load session detail: %s", exc)
        await query.edit_message_text(f"Failed to load session: {exc}")


async def _handle_resume(query, context, session_id: str) -> None:
    """Resume a stopped/paused session and switch the user's active session cache."""
    api_client = context.bot_data["api_client"]
    telegram_id = query.from_user.id

    try:
        await query.edit_message_text("Resuming session...")
        session = await api_client.resume_session(UUID(session_id), telegram_id)

        # Update the bot_data cache so message routing uses the resumed session.
        context.bot_data[f"session:{telegram_id}"] = str(session.id)

        await query.edit_message_text(
            f"Session resumed: {session.container_name}\n"
            "Send a message to continue."
        )
    except Exception as exc:
        logger.exception("Failed to resume session %s: %s", session_id, exc)
        await query.edit_message_text(f"Resume failed: {exc}")


async def _handle_session_history(query, context, session_id: str) -> None:
    """Show message history for a selected session."""
    api_client = context.bot_data["api_client"]

    try:
        messages = await api_client.get_session_messages(UUID(session_id), limit=20)
        text = format_message_history(messages)

        max_length = 4000
        if len(text) > max_length:
            text = text[:max_length] + "\n\n... (truncated)"

        await query.edit_message_text(text)
    except Exception as exc:
        logger.exception("Failed to load session history: %s", exc)
        await query.edit_message_text(f"Failed to load history: {exc}")


async def _handle_sessions_list(query, context) -> None:
    """Re-render the session list (triggered by 'Back to List' button)."""
    api_client = context.bot_data["api_client"]
    telegram_id = query.from_user.id

    try:
        sessions = await api_client.list_user_sessions(telegram_id)
        text = format_session_list_for_user(sessions)

        if not sessions:
            await query.edit_message_text(text)
            return

        button_data = [
            (index, str(session.id), format_session_button_label(session, index))
            for index, session in enumerate(sessions, start=1)
        ]
        await query.edit_message_text(
            text, reply_markup=session_list_keyboard(button_data)
        )
    except Exception as exc:
        logger.exception("Failed to list sessions: %s", exc)
        await query.edit_message_text(f"Failed to list sessions: {exc}")


async def _handle_generic_action(query, context, action: str) -> None:
    """Handle simple info/help actions from keyboards."""
    if action == "sessions_list":
        await _handle_sessions_list(query, context)
        return

    responses = {
        "cancel": "Cancelled.",
        "new_session": "Use the /new command to create a container session.",
        "set_key_help": "Use /setkey <your_api_key> to store your API key.",
        "provider_help": "Use /setprovider anthropic|openrouter|custom to configure your provider.",
    }
    await query.edit_message_text(responses.get(action, f"Action: {action}"))


async def _handle_admin_destroy_session(query, context, session_id: str) -> None:
    """Admin destroys a session from the /containers listing."""
    admin_ids = context.bot_data.get("admin_ids", [])
    if query.from_user.id not in admin_ids:
        await query.edit_message_text("Only admins can destroy sessions.")
        return

    api_client = context.bot_data["api_client"]

    try:
        await api_client.destroy_session(UUID(session_id))
        await query.edit_message_text(f"Session {session_id[:8]}... destroyed.")
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            await query.edit_message_text("Session already destroyed.")
        else:
            logger.exception("Failed to destroy session %s: %s", session_id, exc)
            await query.edit_message_text(f"Destroy failed: {exc}")
    except Exception as exc:
        logger.exception("Failed to destroy session %s: %s", session_id, exc)
        await query.edit_message_text(f"Destroy failed: {exc}")


async def _handle_admin_destroy_by_status(query, context, status: str) -> None:
    """Admin bulk-destroys all sessions with a given status."""
    admin_ids = context.bot_data.get("admin_ids", [])
    if query.from_user.id not in admin_ids:
        await query.edit_message_text("Only admins can destroy sessions.")
        return

    api_client = context.bot_data["api_client"]

    try:
        result = await api_client.destroy_sessions_by_status(status)
        destroyed = result.get("destroyed", 0)
        failed = result.get("failed", 0)

        message = f"Destroyed {destroyed} {status} session(s)."
        if failed:
            message += f"\n{failed} session(s) failed to destroy."
        await query.edit_message_text(message)
    except Exception as exc:
        logger.exception("Failed to bulk destroy %s sessions: %s", status, exc)
        await query.edit_message_text(f"Bulk destroy failed: {exc}")
