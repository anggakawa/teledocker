"""Tests for ClaudeSDKRunner — structured event streaming.

Tests mock the claude_agent_sdk.query() function to simulate various
streaming scenarios: text output, tool use, errors, and session resumption.
"""

from unittest.mock import MagicMock, patch

import pytest
from agent_bridge.sdk_runner import _summarize_tool_input


class FakeStreamEvent:
    """Mimics claude_agent_sdk.types.StreamEvent."""

    def __init__(self, event: dict):
        self.event = event


class FakeInitMessage:
    """Mimics the init message from the SDK."""

    def __init__(self, session_id: str):
        self.subtype = "init"
        self.session_id = session_id


class FakeInitMessageNoSessionId:
    """Mimics a SystemMessage with subtype=init but no session_id attribute.

    This happens with certain SDK message types (e.g. SystemMessage) that
    carry the init subtype but lack a session_id field.
    """

    def __init__(self):
        self.subtype = "init"


class FakeResultMessage:
    """Mimics the result message from the SDK."""

    def __init__(self, session_id: str, cost_usd: float = 0.01, duration_ms: int = 500):
        self.subtype = "result"
        self.session_id = session_id
        self.cost_usd = cost_usd
        self.duration_ms = duration_ms


def _get_patched_runner():
    """Create a ClaudeSDKRunner with SDK imports mocked.

    Returns (runner, patches_context) — use patches as a context manager.
    """
    from agent_bridge.sdk_runner import ClaudeSDKRunner
    return ClaudeSDKRunner()


def _make_patches(mock_query_fn):
    """Create the standard set of patches for SDK runner tests.

    We patch:
    - query: the async generator that yields SDK messages.
    - StreamEvent: so isinstance checks work with our FakeStreamEvent.
    - ClaudeAgentOptions: so we don't hit the real constructor.
    """
    return [
        patch("agent_bridge.sdk_runner.query", side_effect=mock_query_fn),
        patch("agent_bridge.sdk_runner.StreamEvent", FakeStreamEvent),
        patch("agent_bridge.sdk_runner.ClaudeAgentOptions", MagicMock),
    ]


async def _collect_events(runner, prompt: str, env_vars: dict | None = None) -> list[dict]:
    """Helper to collect all events from a runner.send_message call."""
    events = []
    async for event in runner.send_message(prompt, env_vars or {}):
        events.append(event)
    return events


class TestSummarizeToolInput:
    """Tests for the _summarize_tool_input helper."""

    def test_read_tool(self):
        result = _summarize_tool_input("Read", {"file_path": "/workspace/main.py"})
        assert result == "/workspace/main.py"

    def test_write_tool(self):
        result = _summarize_tool_input("Write", {"file_path": "/workspace/out.txt"})
        assert result == "/workspace/out.txt"

    def test_bash_tool_short(self):
        result = _summarize_tool_input("Bash", {"command": "ls -la"})
        assert result == "ls -la"

    def test_bash_tool_long_truncated(self):
        long_cmd = "x" * 100
        result = _summarize_tool_input("Bash", {"command": long_cmd})
        assert len(result) == 80
        assert result.endswith("...")

    def test_glob_tool(self):
        result = _summarize_tool_input("Glob", {"pattern": "**/*.py"})
        assert result == "**/*.py"

    def test_grep_tool(self):
        result = _summarize_tool_input("Grep", {"pattern": "def main"})
        assert result == "def main"

    def test_unknown_tool_with_string_value(self):
        result = _summarize_tool_input("CustomTool", {"query": "something"})
        assert result == "something"

    def test_unknown_tool_empty_input(self):
        result = _summarize_tool_input("CustomTool", {})
        assert result == ""


class TestClaudeSDKRunnerTextStreaming:
    """Tests for basic text streaming through the runner."""

    @pytest.mark.asyncio
    async def test_text_delta_events(self):
        """Text deltas from the SDK should be yielded as text_delta events."""
        runner = _get_patched_runner()

        async def mock_query(prompt, options):
            yield FakeInitMessage("session-123")
            yield FakeStreamEvent({"type": "content_block_delta", "delta": {"type": "text_delta", "text": "Hello "}})
            yield FakeStreamEvent({"type": "content_block_delta", "delta": {"type": "text_delta", "text": "world!"}})

        patches = _make_patches(mock_query)
        for p in patches:
            p.start()
        try:
            events = await _collect_events(runner, "Hi")
        finally:
            for p in patches:
                p.stop()

        text_events = [e for e in events if e["type"] == "text_delta"]
        assert len(text_events) == 2
        assert text_events[0]["text"] == "Hello "
        assert text_events[1]["text"] == "world!"

    @pytest.mark.asyncio
    async def test_session_id_captured(self):
        """Session ID from init message should be stored for resume."""
        runner = _get_patched_runner()

        async def mock_query(prompt, options):
            yield FakeInitMessage("session-abc")

        patches = _make_patches(mock_query)
        for p in patches:
            p.start()
        try:
            await _collect_events(runner, "Hi")
        finally:
            for p in patches:
                p.stop()

        assert runner._session_id == "session-abc"

    @pytest.mark.asyncio
    async def test_result_event_includes_session_id(self):
        """The final result event should include the session ID."""
        runner = _get_patched_runner()

        async def mock_query(prompt, options):
            yield FakeInitMessage("session-xyz")

        patches = _make_patches(mock_query)
        for p in patches:
            p.start()
        try:
            events = await _collect_events(runner, "Hi")
        finally:
            for p in patches:
                p.stop()

        result_events = [e for e in events if e["type"] == "result"]
        assert len(result_events) >= 1
        assert result_events[-1]["session_id"] == "session-xyz"
        assert "duration_ms" in result_events[-1]

    @pytest.mark.asyncio
    async def test_init_message_without_session_id(self):
        """An init message without session_id (SystemMessage) should not crash."""
        runner = _get_patched_runner()

        async def mock_query(prompt, options):
            # SystemMessage has subtype="init" but no session_id attribute.
            yield FakeInitMessageNoSessionId()
            yield FakeStreamEvent({
                "type": "content_block_delta",
                "delta": {"type": "text_delta", "text": "Hello"},
            })

        patches = _make_patches(mock_query)
        for p in patches:
            p.start()
        try:
            events = await _collect_events(runner, "Hi")
        finally:
            for p in patches:
                p.stop()

        # Should not crash; session_id remains None.
        assert runner._session_id is None
        text_events = [e for e in events if e["type"] == "text_delta"]
        assert len(text_events) == 1
        assert text_events[0]["text"] == "Hello"


class TestClaudeSDKRunnerToolEvents:
    """Tests for tool use event transformation."""

    @pytest.mark.asyncio
    async def test_tool_lifecycle(self):
        """Tool start/end events should be emitted from content_block events."""
        runner = _get_patched_runner()

        async def mock_query(prompt, options):
            yield FakeInitMessage("s1")
            # Tool call starts.
            yield FakeStreamEvent({
                "type": "content_block_start",
                "content_block": {"type": "tool_use", "name": "Read"},
            })
            # Tool input streams in.
            yield FakeStreamEvent({
                "type": "content_block_delta",
                "delta": {"type": "input_json_delta", "partial_json": '{"file_path":'},
            })
            yield FakeStreamEvent({
                "type": "content_block_delta",
                "delta": {"type": "input_json_delta", "partial_json": ' "main.py"}'},
            })
            # Tool call complete.
            yield FakeStreamEvent({"type": "content_block_stop"})

        patches = _make_patches(mock_query)
        for p in patches:
            p.start()
        try:
            events = await _collect_events(runner, "Read main.py")
        finally:
            for p in patches:
                p.stop()

        tool_start = [e for e in events if e["type"] == "tool_start"]
        tool_end = [e for e in events if e["type"] == "tool_end"]

        assert len(tool_start) == 1
        assert tool_start[0]["tool_name"] == "Read"

        assert len(tool_end) == 1
        assert tool_end[0]["tool_name"] == "Read"
        assert tool_end[0]["tool_input"] == {"file_path": "main.py"}

    @pytest.mark.asyncio
    async def test_text_block_not_treated_as_tool(self):
        """content_block_start for text blocks should not emit tool_start."""
        runner = _get_patched_runner()

        async def mock_query(prompt, options):
            yield FakeInitMessage("s1")
            yield FakeStreamEvent({
                "type": "content_block_start",
                "content_block": {"type": "text"},
            })
            yield FakeStreamEvent({
                "type": "content_block_delta",
                "delta": {"type": "text_delta", "text": "Hello"},
            })
            yield FakeStreamEvent({"type": "content_block_stop"})

        patches = _make_patches(mock_query)
        for p in patches:
            p.start()
        try:
            events = await _collect_events(runner, "Hi")
        finally:
            for p in patches:
                p.stop()

        tool_events = [e for e in events if e["type"] in ("tool_start", "tool_end")]
        assert len(tool_events) == 0


class TestClaudeSDKRunnerErrorHandling:
    """Tests for error scenarios."""

    @pytest.mark.asyncio
    async def test_sdk_exception_yields_error(self):
        """Exceptions from the SDK should be caught and yielded as error events."""
        runner = _get_patched_runner()

        async def mock_query(prompt, options):
            raise RuntimeError("SDK crashed")
            yield  # Make it an async generator.

        patches = _make_patches(mock_query)
        for p in patches:
            p.start()
        try:
            events = await _collect_events(runner, "Hi")
        finally:
            for p in patches:
                p.stop()

        error_events = [e for e in events if e["type"] == "error"]
        assert len(error_events) == 1
        assert "SDK crashed" in error_events[0]["text"]

    @pytest.mark.asyncio
    async def test_malformed_tool_input_json(self):
        """Invalid JSON in tool input should fall back to raw string."""
        runner = _get_patched_runner()

        async def mock_query(prompt, options):
            yield FakeInitMessage("s1")
            yield FakeStreamEvent({
                "type": "content_block_start",
                "content_block": {"type": "tool_use", "name": "Bash"},
            })
            yield FakeStreamEvent({
                "type": "content_block_delta",
                "delta": {"type": "input_json_delta", "partial_json": "{invalid"},
            })
            yield FakeStreamEvent({"type": "content_block_stop"})

        patches = _make_patches(mock_query)
        for p in patches:
            p.start()
        try:
            events = await _collect_events(runner, "test")
        finally:
            for p in patches:
                p.stop()

        tool_end = [e for e in events if e["type"] == "tool_end"]
        assert len(tool_end) == 1
        assert tool_end[0]["tool_input"] == {"raw": "{invalid"}


class TestClaudeSDKRunnerResultEvent:
    """Tests for final result event emission."""

    @pytest.mark.asyncio
    async def test_result_event_with_sdk_metadata(self):
        """SDK result message metadata should be included in result event."""
        runner = _get_patched_runner()

        async def mock_query(prompt, options):
            yield FakeInitMessage("s1")
            yield FakeStreamEvent({
                "type": "content_block_delta",
                "delta": {"type": "text_delta", "text": "Done"},
            })
            yield FakeResultMessage("s1", cost_usd=0.05, duration_ms=2000)

        patches = _make_patches(mock_query)
        for p in patches:
            p.start()
        try:
            events = await _collect_events(runner, "Hi")
        finally:
            for p in patches:
                p.stop()

        result_events = [e for e in events if e["type"] == "result"]
        # At least the SDK result + our timing result.
        assert len(result_events) >= 1

        # Check the SDK-sourced result.
        sdk_result = result_events[0]
        assert sdk_result["cost_usd"] == 0.05
        assert sdk_result["session_id"] == "s1"

    @pytest.mark.asyncio
    async def test_timing_result_always_emitted(self):
        """Our own timing result should always be emitted at the end."""
        runner = _get_patched_runner()

        async def mock_query(prompt, options):
            yield FakeInitMessage("s1")

        patches = _make_patches(mock_query)
        for p in patches:
            p.start()
        try:
            events = await _collect_events(runner, "Hi")
        finally:
            for p in patches:
                p.stop()

        # The last event should be our timing result.
        last_event = events[-1]
        assert last_event["type"] == "result"
        assert "duration_ms" in last_event
        assert last_event["duration_ms"] >= 0


class TestClaudeSDKRunnerModelSelection:
    """Tests for ANTHROPIC_MODEL env var extraction and SDK model option."""

    @pytest.mark.asyncio
    async def test_model_passed_to_options(self):
        """ANTHROPIC_MODEL from env_vars should be passed as model= to ClaudeAgentOptions."""
        runner = _get_patched_runner()

        async def mock_query(prompt, options):
            yield FakeInitMessage("s1")

        mock_options_cls = MagicMock()

        patches = [
            patch("agent_bridge.sdk_runner.query", side_effect=mock_query),
            patch("agent_bridge.sdk_runner.StreamEvent", FakeStreamEvent),
            patch("agent_bridge.sdk_runner.ClaudeAgentOptions", mock_options_cls),
        ]
        for p in patches:
            p.start()
        try:
            env_vars = {"ANTHROPIC_API_KEY": "sk-test", "ANTHROPIC_MODEL": "opus"}
            await _collect_events(runner, "Hi", env_vars)
        finally:
            for p in patches:
                p.stop()

        # ClaudeAgentOptions should have been called with model="opus".
        mock_options_cls.assert_called_once()
        call_kwargs = mock_options_cls.call_args
        assert call_kwargs.kwargs["model"] == "opus"

    @pytest.mark.asyncio
    async def test_model_none_when_not_in_env(self):
        """When ANTHROPIC_MODEL is absent, model=None should be passed to options."""
        runner = _get_patched_runner()

        async def mock_query(prompt, options):
            yield FakeInitMessage("s1")

        mock_options_cls = MagicMock()

        patches = [
            patch("agent_bridge.sdk_runner.query", side_effect=mock_query),
            patch("agent_bridge.sdk_runner.StreamEvent", FakeStreamEvent),
            patch("agent_bridge.sdk_runner.ClaudeAgentOptions", mock_options_cls),
        ]
        for p in patches:
            p.start()
        try:
            env_vars = {"ANTHROPIC_API_KEY": "sk-test"}
            await _collect_events(runner, "Hi", env_vars)
        finally:
            for p in patches:
                p.stop()

        mock_options_cls.assert_called_once()
        call_kwargs = mock_options_cls.call_args
        assert call_kwargs.kwargs["model"] is None

    @pytest.mark.asyncio
    async def test_model_removed_from_env_vars(self):
        """ANTHROPIC_MODEL should be popped from env_vars before passing to SDK."""
        runner = _get_patched_runner()

        async def mock_query(prompt, options):
            yield FakeInitMessage("s1")

        mock_options_cls = MagicMock()

        patches = [
            patch("agent_bridge.sdk_runner.query", side_effect=mock_query),
            patch("agent_bridge.sdk_runner.StreamEvent", FakeStreamEvent),
            patch("agent_bridge.sdk_runner.ClaudeAgentOptions", mock_options_cls),
        ]
        for p in patches:
            p.start()
        try:
            env_vars = {"ANTHROPIC_API_KEY": "sk-test", "ANTHROPIC_MODEL": "haiku"}
            await _collect_events(runner, "Hi", env_vars)
        finally:
            for p in patches:
                p.stop()

        # env dict passed to options should NOT contain ANTHROPIC_MODEL.
        call_kwargs = mock_options_cls.call_args
        assert "ANTHROPIC_MODEL" not in call_kwargs.kwargs["env"]
        assert call_kwargs.kwargs["env"]["ANTHROPIC_API_KEY"] == "sk-test"
