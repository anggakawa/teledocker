"""Claude Agent SDK wrapper for structured streaming inside containers.

Replaces the raw CLI subprocess approach (claude.py) with the official
Python Agent SDK, which provides:
- Persistent multi-turn sessions (no process restart per message)
- Structured streaming events (text deltas, tool use, results)
- Session ID tracking for conversation continuity
- Interrupt support for cancelling long-running operations
"""

import json
import logging
import time
from collections.abc import AsyncGenerator
from typing import Any

from claude_agent_sdk import ClaudeAgentOptions, query
from claude_agent_sdk.types import StreamEvent

logger = logging.getLogger(__name__)


def _summarize_tool_input(tool_name: str, tool_input: dict[str, Any]) -> str:
    """Build a short human-readable summary of a tool call's input.

    Used for the tool_end event so downstream layers can display
    what Claude is doing without parsing raw JSON.
    """
    if tool_name == "Read":
        return tool_input.get("file_path", "")
    if tool_name in ("Write", "Edit"):
        return tool_input.get("file_path", "")
    if tool_name == "Bash":
        command = tool_input.get("command", "")
        # Truncate long commands for display.
        if len(command) > 80:
            return command[:77] + "..."
        return command
    if tool_name == "Glob":
        return tool_input.get("pattern", "")
    if tool_name == "Grep":
        return tool_input.get("pattern", "")
    # Fallback: show first key's value or empty.
    for value in tool_input.values():
        if isinstance(value, str):
            return value[:80] if len(str(value)) > 80 else str(value)
    return ""


class ClaudeSDKRunner:
    """Manages Claude Agent SDK sessions with structured event streaming."""

    def __init__(self) -> None:
        # Session ID from the SDK, used to resume multi-turn conversations.
        self._session_id: str | None = None

    async def send_message(
        self, prompt: str, env_vars: dict[str, str]
    ) -> AsyncGenerator[dict, None]:
        """Send a prompt and yield structured event dicts.

        Each yielded dict has a "type" key matching StreamEventType values:
        - text_delta: {"type": "text_delta", "text": "..."}
        - tool_start: {"type": "tool_start", "tool_name": "Read"}
        - tool_end:   {"type": "tool_end", "tool_name": "Read", "tool_input": {...}}
        - tool_result: {"type": "tool_result", "tool_name": "Read",
                        "tool_result_summary": "...", "is_error": false}
        - result:     {"type": "result", "session_id": "...", "cost_usd": ...,
                       "duration_ms": ...}
        - error:      {"type": "error", "text": "..."}

        Args:
            prompt: The user's message.
            env_vars: Environment variables to inject (API keys, provider config).
                      Passed directly to the SDK via the env option.
        """
        start_time = time.monotonic()

        try:
            async for event_dict in self._run_query(prompt, env_vars):
                yield event_dict
        except Exception as exc:
            logger.exception("SDK query failed: %s", exc)
            yield {"type": "error", "text": str(exc)}

        # Emit final result event with timing.
        elapsed_ms = int((time.monotonic() - start_time) * 1000)
        yield {
            "type": "result",
            "session_id": self._session_id or "",
            "duration_ms": elapsed_ms,
        }

    async def _run_query(
        self, prompt: str, env_vars: dict[str, str]
    ) -> AsyncGenerator[dict, None]:
        """Execute the SDK query and transform raw API events into our event format."""
        options = ClaudeAgentOptions(
            include_partial_messages=True,
            permission_mode="bypassPermissions",
            max_turns=100,
            env=env_vars,
        )

        # Resume conversation if we have a session from a previous turn.
        if self._session_id:
            options.resume = self._session_id

        # State tracking for tool call accumulation.
        current_tool_name: str | None = None
        current_tool_input_json = ""

        async for message in query(prompt=prompt, options=options):
            # Capture session ID from the init message.
            if hasattr(message, "subtype") and message.subtype == "init":
                self._session_id = message.session_id
                continue

            # Handle raw streaming events (text deltas, tool call lifecycle).
            if isinstance(message, StreamEvent):
                event = message.event
                event_type = event.get("type", "")

                if event_type == "content_block_start":
                    content_block = event.get("content_block", {})
                    block_type = content_block.get("type", "")

                    if block_type == "tool_use":
                        current_tool_name = content_block.get("name", "unknown")
                        current_tool_input_json = ""
                        yield {
                            "type": "tool_start",
                            "tool_name": current_tool_name,
                        }

                elif event_type == "content_block_delta":
                    delta = event.get("delta", {})
                    delta_type = delta.get("type", "")

                    if delta_type == "text_delta":
                        text = delta.get("text", "")
                        if text:
                            yield {"type": "text_delta", "text": text}

                    elif delta_type == "input_json_delta":
                        # Accumulate tool input JSON chunks.
                        current_tool_input_json += delta.get("partial_json", "")

                elif event_type == "content_block_stop":
                    # Tool call fully formed — emit tool_end with parsed input.
                    if current_tool_name:
                        tool_input = {}
                        if current_tool_input_json:
                            try:
                                tool_input = json.loads(current_tool_input_json)
                            except json.JSONDecodeError:
                                tool_input = {"raw": current_tool_input_json}

                        yield {
                            "type": "tool_end",
                            "tool_name": current_tool_name,
                            "tool_input": tool_input,
                        }
                        current_tool_name = None
                        current_tool_input_json = ""

                continue

            # Handle complete assistant messages (contain tool results).
            if hasattr(message, "content") and hasattr(message, "role"):
                # Tool result messages have role="user" with tool_result blocks.
                if message.role == "user":
                    for block in message.content:
                        if hasattr(block, "type") and block.type == "tool_result":
                            tool_name = getattr(block, "tool_use_id", "")
                            is_error = getattr(block, "is_error", False)
                            # Summarize the result content.
                            content = getattr(block, "content", "")
                            if isinstance(content, list):
                                parts = []
                                for part in content:
                                    if hasattr(part, "text"):
                                        parts.append(part.text)
                                content = " ".join(parts)
                            summary = str(content)[:200] if content else ""

                            yield {
                                "type": "tool_result",
                                "tool_name": tool_name,
                                "tool_result_summary": summary,
                                "is_error": is_error,
                            }

            # Handle final result message with metadata.
            if hasattr(message, "subtype") and message.subtype == "result":
                cost_usd = None
                if hasattr(message, "cost_usd"):
                    cost_usd = message.cost_usd
                duration_ms = None
                if hasattr(message, "duration_ms"):
                    duration_ms = message.duration_ms
                if hasattr(message, "session_id"):
                    self._session_id = message.session_id

                yield {
                    "type": "result",
                    "session_id": self._session_id or "",
                    "cost_usd": cost_usd,
                    "duration_ms": duration_ms,
                }
