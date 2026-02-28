"""Claude Agent SDK wrapper for structured streaming inside containers.

Replaces the raw CLI subprocess approach (claude.py) with the official
Python Agent SDK, which provides:
- Persistent multi-turn sessions (no process restart per message)
- Structured streaming events (text deltas, tool use, results)
- Session ID tracking for conversation continuity
- File-based session persistence across container restarts
- Concurrency lock for safe shared-runner access
"""

import asyncio
import json
import logging
import time
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any

from claude_agent_sdk import ClaudeAgentOptions, ProcessError, query
from claude_agent_sdk.types import StreamEvent

from agent_bridge.mcp_builder import build_mcp_servers

logger = logging.getLogger(__name__)

# Session ID is persisted to disk so it survives container destroy/recreate cycles.
# Stored on the persistent /workspace volume (the only storage that outlives containers).
_SESSION_FILE = Path("/workspace/.agent_session")

# Claude CLI state directory on the persistent volume. ~/.claude is a symlink
# pointing here. Must exist before the CLI runs — a dangling symlink blocks
# mkdir -p from creating subdirectories at runtime.
_CLAUDE_STATE_DIR = Path("/workspace/.claude")


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
    """Manages Claude Agent SDK sessions with structured event streaming.

    Designed to be used as a singleton per container. Each container serves
    one user, so there is no multi-tenancy concern. The asyncio lock serializes
    concurrent messages to prevent session file corruption.
    """

    def __init__(self) -> None:
        self._ensure_claude_state_dir()
        # Session ID from the SDK, used to resume multi-turn conversations.
        self._session_id: str | None = self._load_session_id()
        # Serialize access — SDK can't handle concurrent query() on the same session.
        self._lock = asyncio.Lock()
        # Cooperative cancellation flag — checked between SDK events in _run_query().
        self._cancel_event = asyncio.Event()

    @staticmethod
    def _ensure_claude_state_dir() -> None:
        """Create the Claude CLI state directory if it doesn't exist.

        ~/.claude is a symlink to /workspace/.claude. If the target directory
        is missing (e.g. existing volume from an older image), the CLI silently
        fails to persist session data because mkdir -p can't resolve through a
        dangling symlink.
        """
        try:
            _CLAUDE_STATE_DIR.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            logger.warning("Failed to create Claude state dir %s: %s", _CLAUDE_STATE_DIR, exc)

    @staticmethod
    def _load_session_id() -> str | None:
        """Load session ID from disk if a previous session file exists."""
        try:
            if _SESSION_FILE.exists():
                session_id = _SESSION_FILE.read_text().strip()
                if session_id:
                    logger.info("Loaded session ID from %s", _SESSION_FILE)
                    return session_id
        except (OSError, ValueError) as exc:
            logger.warning("Failed to load session file %s: %s", _SESSION_FILE, exc)
        return None

    def _save_session_id(self) -> None:
        """Persist current session ID to disk for container restart survival."""
        if not self._session_id:
            return
        try:
            _SESSION_FILE.write_text(self._session_id)
        except OSError as exc:
            logger.warning("Failed to save session file %s: %s", _SESSION_FILE, exc)

    def clear_session(self) -> None:
        """Reset conversation state — starts a fresh session on next message."""
        self._session_id = None
        try:
            _SESSION_FILE.unlink(missing_ok=True)
        except OSError as exc:
            logger.warning("Failed to delete session file: %s", exc)

    def cancel(self) -> None:
        """Signal the running query to stop at the next checkpoint."""
        self._cancel_event.set()

    def get_session_info(self) -> dict:
        """Return current session state for introspection."""
        return {
            "session_id": self._session_id,
            "has_session": self._session_id is not None,
            "session_file": str(_SESSION_FILE),
        }

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
        self._cancel_event.clear()

        async with self._lock:
            start_time = time.monotonic()

            try:
                async for event_dict in self._run_query(prompt, env_vars):
                    yield event_dict
            except ProcessError as exc:
                # Resume may fail if the session expired or was corrupted.
                # Retry once without resume before giving up.
                if self._session_id:
                    logger.warning(
                        "Resume failed (session %s): %s — retrying without resume",
                        self._session_id,
                        exc,
                    )
                    self.clear_session()
                    try:
                        async for event_dict in self._run_query(prompt, env_vars):
                            yield event_dict
                    except Exception as retry_exc:
                        logger.exception("Retry without resume also failed: %s", retry_exc)
                        yield {"type": "error", "text": str(retry_exc)}
                else:
                    logger.exception("SDK query failed: %s", exc)
                    yield {"type": "error", "text": str(exc)}
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
        # Extract model selection before passing env vars to the SDK.
        # ANTHROPIC_MODEL is our internal transport — the SDK uses the model option directly.
        model = env_vars.pop("ANTHROPIC_MODEL", None)

        # Build MCP server configs from registry, gated by available env vars.
        mcp_servers = build_mcp_servers(env_vars)

        options = ClaudeAgentOptions(
            include_partial_messages=True,
            permission_mode="bypassPermissions",
            max_turns=100,
            env=env_vars,
            model=model,
            mcp_servers=mcp_servers if mcp_servers else None,
        )

        # Resume conversation if we have a session from a previous turn.
        if self._session_id:
            options.resume = self._session_id

        # State tracking for tool call accumulation.
        current_tool_name: str | None = None
        current_tool_input_json = ""

        async for message in query(prompt=prompt, options=options):
            # Cooperative cancellation — check between events so we stop promptly.
            if self._cancel_event.is_set():
                logger.info("Query cancelled by user")
                yield {"type": "error", "text": "Cancelled by user."}
                return

            # Skip system/init messages (no session_id on these in SDK v0.1.44+).
            if hasattr(message, "subtype") and message.subtype == "init":
                continue

            # Handle raw streaming events (text deltas, tool call lifecycle).
            if isinstance(message, StreamEvent):
                # Capture session ID from the first StreamEvent — the SDK
                # embeds it on every event rather than a separate init message.
                if not self._session_id and message.session_id:
                    self._session_id = message.session_id
                    self._save_session_id()
                    logger.info("Captured session ID: %s", self._session_id)

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
            # SDK v0.1.44 uses subtype="success" (not "result").
            if hasattr(message, "subtype") and message.subtype in ("result", "success"):
                cost_usd = None
                if hasattr(message, "total_cost_usd"):
                    cost_usd = message.total_cost_usd
                duration_ms = None
                if hasattr(message, "duration_ms"):
                    duration_ms = message.duration_ms
                if hasattr(message, "session_id"):
                    self._session_id = message.session_id
                    self._save_session_id()

                yield {
                    "type": "result",
                    "session_id": self._session_id or "",
                    "cost_usd": cost_usd,
                    "duration_ms": duration_ms,
                }
