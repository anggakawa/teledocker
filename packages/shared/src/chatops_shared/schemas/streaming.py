"""Structured streaming event types shared across all services.

Events flow from the agent bridge (innermost layer) through container-manager
and api-server to the telegram-bot (outermost layer). Each layer passes events
through with minimal transformation, using these shared types as the contract.
"""

from enum import StrEnum
from typing import Any

from pydantic import BaseModel


class StreamEventType(StrEnum):
    """Event types emitted during a streaming AI response."""

    # Incremental text output from the model.
    TEXT_DELTA = "text_delta"

    # Claude started forming a tool call (tool name known).
    TOOL_START = "tool_start"

    # Tool call input fully formed (tool name + input available).
    TOOL_END = "tool_end"

    # Tool execution finished (result summary available).
    TOOL_RESULT = "tool_result"

    # Final result with metadata (cost, duration, session_id).
    RESULT = "result"

    # An error occurred during processing.
    ERROR = "error"


class StreamEvent(BaseModel):
    """A single structured event in a streaming response.

    The `type` field determines which data fields are populated:
    - text_delta: text
    - tool_start: tool_name
    - tool_end: tool_name, tool_input
    - tool_result: tool_name, tool_result_summary, is_error
    - result: session_id, cost_usd, duration_ms
    - error: text
    """

    type: StreamEventType
    text: str | None = None
    tool_name: str | None = None
    tool_input: dict[str, Any] | None = None
    tool_result_summary: str | None = None
    is_error: bool | None = None
    session_id: str | None = None
    cost_usd: float | None = None
    duration_ms: int | None = None
