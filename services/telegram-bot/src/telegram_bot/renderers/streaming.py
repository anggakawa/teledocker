"""Real-time streaming renderer for Telegram messages.

Manages live-editing a Telegram message as structured events arrive,
showing text incrementally and tool activity as status lines.

Telegram constraints that shape this design:
- editMessageText rate limit: ~1 call/second per chat is safe.
- Max message length: 4096 characters. When exceeded, start a new message.
- editMessageText fails if the new text is identical to the old one.
"""

import asyncio
import logging
import time

from telegram import Bot, Message
from telegram.error import BadRequest, TimedOut

from telegram_bot.markdown_to_telegram import markdown_to_telegram_html

logger = logging.getLogger(__name__)

# Telegram message character limit.
_MAX_MESSAGE_LENGTH = 4096

# Minimum interval between message edits (seconds).
_EDIT_INTERVAL_SECONDS = 1.0

# Buffer size threshold that triggers an early edit.
_BUFFER_FLUSH_THRESHOLD = 100

# Safety margin to leave room for tool status lines in the message.
_LENGTH_SAFETY_MARGIN = 200

# How tool names map to human-readable action descriptions.
_TOOL_DISPLAY_NAMES: dict[str, str] = {
    "Read": "Reading",
    "Write": "Writing",
    "Edit": "Editing",
    "Bash": "Running",
    "Glob": "Searching",
    "Grep": "Searching for",
    "WebFetch": "Fetching",
    "WebSearch": "Searching web for",
    "Task": "Running task",
}


def _format_tool_status(tool_name: str, tool_input: dict | None = None) -> str:
    """Build a human-readable tool status line.

    Examples:
        > Reading src/main.py...
        > Running: pytest tests/...
        > Searching: **/*.py...
    """
    action = _TOOL_DISPLAY_NAMES.get(tool_name, f"Using {tool_name}")

    if tool_input is None:
        return f"> {action}..."

    if tool_name == "Read":
        file_path = tool_input.get("file_path", "")
        return f"> {action} {file_path}..."
    if tool_name in ("Write", "Edit"):
        file_path = tool_input.get("file_path", "")
        return f"> {action} {file_path}..."
    if tool_name == "Bash":
        command = tool_input.get("command", "")
        if len(command) > 60:
            command = command[:57] + "..."
        return f"> {action}: {command}..."
    if tool_name == "Glob":
        pattern = tool_input.get("pattern", "")
        return f"> {action}: {pattern}..."
    if tool_name == "Grep":
        pattern = tool_input.get("pattern", "")
        return f"> {action}: {pattern}..."

    return f"> {action}..."


class TelegramStreamRenderer:
    """Manages real-time editing of a Telegram message as events stream in.

    Usage:
        renderer = TelegramStreamRenderer(bot, chat_id)
        await renderer.start()

        for event in events:
            await renderer.handle_event(event)

        await renderer.finalize()
    """

    def __init__(self, bot: Bot, chat_id: int) -> None:
        self._bot = bot
        self._chat_id = chat_id

        # The current message being edited.
        self._message: Message | None = None
        # All messages sent during this response (for overflow).
        self._messages: list[Message] = []

        # Accumulated text content (only text_delta, not tool status).
        self._text_buffer: str = ""
        # Pending text that hasn't been flushed yet.
        self._pending_text: str = ""
        # Current tool status line (shown below text).
        self._tool_status: str = ""

        # Last time we edited the message (for rate limiting).
        self._last_edit_time: float = 0.0
        # The text content last sent to Telegram (to avoid no-op edits).
        self._last_sent_text: str = ""

        # Whether an error occurred (changes finalize behavior).
        self._has_error: bool = False

    async def start(self) -> None:
        """Send the initial placeholder message."""
        self._message = await self._bot.send_message(
            chat_id=self._chat_id,
            text="...",
        )
        self._messages.append(self._message)
        self._last_sent_text = "..."
        self._last_edit_time = time.monotonic()

    async def handle_event(self, event: dict) -> None:
        """Process a single structured streaming event.

        Args:
            event: Dict with "type" key and type-specific data.
        """
        event_type = event.get("type", "")

        if event_type == "text_delta":
            text = event.get("text", "")
            if text:
                self._text_buffer += text
                self._pending_text += text
                await self._maybe_flush()

        elif event_type == "tool_start":
            tool_name = event.get("tool_name", "unknown")
            self._tool_status = _format_tool_status(tool_name)
            await self._force_flush()

        elif event_type == "tool_end":
            tool_name = event.get("tool_name", "unknown")
            tool_input = event.get("tool_input", {})
            self._tool_status = _format_tool_status(tool_name, tool_input)
            await self._force_flush()

        elif event_type == "tool_result":
            # Clear tool status when tool execution finishes.
            self._tool_status = ""
            await self._maybe_flush()

        elif event_type == "error":
            error_text = event.get("text", "Unknown error")
            self._text_buffer += f"\n\nError: {error_text}"
            self._pending_text += f"\n\nError: {error_text}"
            self._has_error = True
            await self._force_flush()

        elif event_type == "result":
            # Final metadata event — nothing to display right now.
            pass

    async def finalize(self) -> None:
        """Flush remaining content and clean up tool status."""
        self._tool_status = ""
        await self._force_flush()

        # If we never received any text, update the placeholder.
        if not self._text_buffer.strip():
            await self._edit_message("(no response from agent)")

    def get_full_response(self) -> str:
        """Return the complete accumulated text response."""
        return self._text_buffer

    async def _maybe_flush(self) -> None:
        """Flush pending text to Telegram if rate limit allows or buffer is large."""
        now = time.monotonic()
        elapsed = now - self._last_edit_time

        should_flush = (
            elapsed >= _EDIT_INTERVAL_SECONDS
            or len(self._pending_text) >= _BUFFER_FLUSH_THRESHOLD
        )

        if should_flush and (self._pending_text or self._tool_status != ""):
            await self._do_flush()

    async def _force_flush(self) -> None:
        """Flush immediately regardless of rate limit timing."""
        # Respect a minimum interval to avoid Telegram 429 errors.
        now = time.monotonic()
        elapsed = now - self._last_edit_time
        if elapsed < 0.3:
            await asyncio.sleep(0.3 - elapsed)

        await self._do_flush()

    async def _do_flush(self) -> None:
        """Actually edit the Telegram message with current content."""
        display_text = self._text_buffer.strip()

        # Append tool status line if active.
        if self._tool_status:
            display_text = f"{display_text}\n\n{self._tool_status}" if display_text else self._tool_status

        if not display_text:
            return

        # Convert Markdown to HTML for Telegram formatting.
        html_text = markdown_to_telegram_html(display_text)

        # Check overflow against HTML length (tags count toward 4096 limit).
        if len(html_text) > _MAX_MESSAGE_LENGTH - _LENGTH_SAFETY_MARGIN:
            await self._handle_overflow()
            return

        await self._edit_message(html_text)
        self._pending_text = ""

    async def _handle_overflow(self) -> None:
        """Handle text exceeding Telegram's message limit.

        Finalize the current message with text up to the limit,
        then start a new message for the remainder.
        """
        # Find a safe split point in the raw accumulated text.
        safe_limit = _MAX_MESSAGE_LENGTH - _LENGTH_SAFETY_MARGIN
        split_text = self._text_buffer[:safe_limit]

        # Try to split at the last newline for cleanliness.
        last_newline = split_text.rfind("\n")
        if last_newline > safe_limit // 2:
            split_point = last_newline
        else:
            split_point = safe_limit

        # Finalize current message with text up to split point.
        finalized_text = self._text_buffer[:split_point].strip()
        finalized_html = markdown_to_telegram_html(finalized_text)
        await self._edit_message(finalized_html)

        # Start a new message with the remainder.
        remainder = self._text_buffer[split_point:].strip()
        self._text_buffer = remainder
        self._pending_text = ""

        remainder_html = markdown_to_telegram_html(remainder) if remainder else "..."
        try:
            self._message = await self._bot.send_message(
                chat_id=self._chat_id,
                text=remainder_html,
                parse_mode="HTML",
            )
            self._messages.append(self._message)
            self._last_sent_text = remainder_html
            self._last_edit_time = time.monotonic()
        except BadRequest as exc:
            if "can't parse entities" in str(exc).lower():
                # HTML was malformed — fall back to plain text.
                logger.warning("Overflow HTML parse failed, falling back to plain text")
                self._message = await self._bot.send_message(
                    chat_id=self._chat_id,
                    text=remainder if remainder else "...",
                )
                self._messages.append(self._message)
                self._last_sent_text = remainder if remainder else "..."
                self._last_edit_time = time.monotonic()
            else:
                logger.exception("Failed to send overflow message")
        except Exception:
            logger.exception("Failed to send overflow message")

    async def _edit_message(self, text: str) -> None:
        """Edit the current Telegram message, handling common errors.

        Sends with parse_mode="HTML". If Telegram rejects the HTML as
        malformed, falls back to plain text for that single edit.
        """
        if self._message is None:
            return

        # Telegram rejects edits where text hasn't changed.
        if text == self._last_sent_text:
            return

        try:
            await self._bot.edit_message_text(
                chat_id=self._chat_id,
                message_id=self._message.message_id,
                text=text,
                parse_mode="HTML",
            )
            self._last_sent_text = text
            self._last_edit_time = time.monotonic()
        except BadRequest as exc:
            error_msg = str(exc).lower()
            if "can't parse entities" in error_msg:
                # HTML was malformed mid-stream — fall back to plain text.
                logger.debug("HTML parse failed, falling back to plain text")
                try:
                    await self._bot.edit_message_text(
                        chat_id=self._chat_id,
                        message_id=self._message.message_id,
                        text=text,
                    )
                    self._last_sent_text = text
                    self._last_edit_time = time.monotonic()
                except Exception:
                    logger.warning("Plain text fallback also failed")
            elif "message is not modified" in error_msg:
                # Content identical — safe to ignore.
                pass
            elif "message to edit not found" in error_msg:
                logger.warning("Message was deleted, sending new one")
                self._message = None
            else:
                logger.warning("Failed to edit message: %s", exc)
        except TimedOut:
            logger.warning("Telegram edit timed out, will retry on next flush")
        except Exception:
            logger.exception("Unexpected error editing message")
