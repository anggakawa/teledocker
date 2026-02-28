"""Tests for ClaudeSDKRunner — structured event streaming.

Tests mock the claude_agent_sdk.query() function to simulate various
streaming scenarios: text output, tool use, errors, session resumption,
file-based persistence, resume failure recovery, and concurrency locking.
"""

import asyncio
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from claude_agent_sdk import ProcessError

from agent_bridge.sdk_runner import _SESSION_FILE, _summarize_tool_input


class FakeStreamEvent:
    """Mimics claude_agent_sdk.types.StreamEvent.

    SDK v0.1.44+ embeds session_id on every StreamEvent (not on a separate
    init message). The runner captures it from the first event.
    """

    def __init__(self, event: dict, session_id: str = ""):
        self.event = event
        self.session_id = session_id


class FakeSystemMessage:
    """Mimics SystemMessage with subtype=init but no session_id.

    SDK v0.1.44 may yield these before StreamEvents. The runner skips them.
    """

    def __init__(self):
        self.subtype = "init"


class FakeResultMessage:
    """Mimics ResultMessage from SDK v0.1.44.

    Uses subtype="success" (not "result") and total_cost_usd (not cost_usd).
    """

    def __init__(
        self, session_id: str, total_cost_usd: float = 0.01, duration_ms: int = 500
    ):
        self.subtype = "success"
        self.session_id = session_id
        self.total_cost_usd = total_cost_usd
        self.duration_ms = duration_ms


def _get_patched_runner(tmp_path: Path | None = None):
    """Create a ClaudeSDKRunner with session file pointed at a temp directory.

    Always patches _SESSION_FILE to avoid touching the real home directory
    and to prevent state leaking between tests.

    Args:
        tmp_path: If provided, session file is redirected here. Otherwise
                  a non-existent path is used (fresh session every time).
    """
    import tempfile
    from agent_bridge.sdk_runner import ClaudeSDKRunner

    if tmp_path is None:
        # Create a unique temp dir so each runner gets an isolated session file.
        tmp_path = Path(tempfile.mkdtemp())

    fake_file = tmp_path / ".agent_session"
    with patch("agent_bridge.sdk_runner._SESSION_FILE", fake_file):
        runner = ClaudeSDKRunner()
        # Store ref so tests can inspect the file.
        runner._test_session_file = fake_file
        return runner


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
            yield FakeStreamEvent({"type": "message_start"}, session_id="session-123")
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
            yield FakeStreamEvent({"type": "message_start"}, session_id="session-abc")

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
            yield FakeStreamEvent({"type": "message_start"}, session_id="session-xyz")

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
            yield FakeSystemMessage()
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
            yield FakeStreamEvent({"type": "message_start"}, session_id="s1")
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
            yield FakeStreamEvent({"type": "message_start"}, session_id="s1")
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
            yield FakeStreamEvent({"type": "message_start"}, session_id="s1")
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
            yield FakeStreamEvent({"type": "message_start"}, session_id="s1")
            yield FakeStreamEvent({
                "type": "content_block_delta",
                "delta": {"type": "text_delta", "text": "Done"},
            })
            yield FakeResultMessage("s1", total_cost_usd=0.05, duration_ms=2000)

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
            yield FakeStreamEvent({"type": "message_start"}, session_id="s1")

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
            yield FakeStreamEvent({"type": "message_start"}, session_id="s1")

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
            yield FakeStreamEvent({"type": "message_start"}, session_id="s1")

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
            yield FakeStreamEvent({"type": "message_start"}, session_id="s1")

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


class TestClaudeSDKRunnerMcpInjection:
    """Tests for MCP server injection into ClaudeAgentOptions."""

    @pytest.mark.asyncio
    async def test_mcp_servers_passed_when_servers_qualify(self):
        """When build_mcp_servers returns servers, they should be passed to options."""
        runner = _get_patched_runner()

        async def mock_query(prompt, options):
            yield FakeStreamEvent({"type": "message_start"}, session_id="s1")

        mock_options_cls = MagicMock()
        fake_mcp_servers = {"github": {"command": "npx", "args": ["-y", "server-github"]}}

        patches = [
            patch("agent_bridge.sdk_runner.query", side_effect=mock_query),
            patch("agent_bridge.sdk_runner.StreamEvent", FakeStreamEvent),
            patch("agent_bridge.sdk_runner.ClaudeAgentOptions", mock_options_cls),
            patch(
                "agent_bridge.sdk_runner.build_mcp_servers",
                return_value=fake_mcp_servers,
            ),
        ]
        for p in patches:
            p.start()
        try:
            await _collect_events(runner, "Hi", {"ANTHROPIC_API_KEY": "sk-test"})
        finally:
            for p in patches:
                p.stop()

        call_kwargs = mock_options_cls.call_args
        assert call_kwargs.kwargs["mcp_servers"] == fake_mcp_servers

    @pytest.mark.asyncio
    async def test_mcp_servers_none_when_no_servers_qualify(self):
        """When build_mcp_servers returns empty dict, mcp_servers should be None."""
        runner = _get_patched_runner()

        async def mock_query(prompt, options):
            yield FakeStreamEvent({"type": "message_start"}, session_id="s1")

        mock_options_cls = MagicMock()

        patches = [
            patch("agent_bridge.sdk_runner.query", side_effect=mock_query),
            patch("agent_bridge.sdk_runner.StreamEvent", FakeStreamEvent),
            patch("agent_bridge.sdk_runner.ClaudeAgentOptions", mock_options_cls),
            patch(
                "agent_bridge.sdk_runner.build_mcp_servers",
                return_value={},
            ),
        ]
        for p in patches:
            p.start()
        try:
            await _collect_events(runner, "Hi", {"ANTHROPIC_API_KEY": "sk-test"})
        finally:
            for p in patches:
                p.stop()

        call_kwargs = mock_options_cls.call_args
        assert call_kwargs.kwargs["mcp_servers"] is None

    @pytest.mark.asyncio
    async def test_mcp_builder_receives_env_vars(self):
        """build_mcp_servers should receive the env_vars dict (after model extraction)."""
        runner = _get_patched_runner()

        async def mock_query(prompt, options):
            yield FakeStreamEvent({"type": "message_start"}, session_id="s1")

        mock_options_cls = MagicMock()
        mock_builder = MagicMock(return_value={})

        patches = [
            patch("agent_bridge.sdk_runner.query", side_effect=mock_query),
            patch("agent_bridge.sdk_runner.StreamEvent", FakeStreamEvent),
            patch("agent_bridge.sdk_runner.ClaudeAgentOptions", mock_options_cls),
            patch("agent_bridge.sdk_runner.build_mcp_servers", mock_builder),
        ]
        for p in patches:
            p.start()
        try:
            env_vars = {
                "ANTHROPIC_API_KEY": "sk-test",
                "GITHUB_TOKEN": "ghp_abc",
                "ANTHROPIC_MODEL": "opus",
            }
            await _collect_events(runner, "Hi", env_vars)
        finally:
            for p in patches:
                p.stop()

        # Builder should have been called with env_vars (model already popped).
        mock_builder.assert_called_once()
        builder_env = mock_builder.call_args[0][0]
        assert builder_env["GITHUB_TOKEN"] == "ghp_abc"
        assert builder_env["ANTHROPIC_API_KEY"] == "sk-test"
        # ANTHROPIC_MODEL should already be popped before builder is called.
        assert "ANTHROPIC_MODEL" not in builder_env


class TestSessionPersistence:
    """Tests for file-based session ID persistence across runner restarts."""

    def test_session_saved_to_file(self, tmp_path):
        """After a query that yields a session ID, it should be written to disk."""
        session_file = tmp_path / ".agent_session"
        runner = _get_patched_runner(tmp_path)

        # Simulate what _save_session_id does.
        runner._session_id = "session-persist-1"
        with patch("agent_bridge.sdk_runner._SESSION_FILE", session_file):
            runner._save_session_id()

        assert session_file.read_text() == "session-persist-1"

    def test_session_loaded_from_file(self, tmp_path):
        """A runner should load session ID from disk on construction."""
        session_file = tmp_path / ".agent_session"
        session_file.write_text("session-from-disk")

        with patch("agent_bridge.sdk_runner._SESSION_FILE", session_file):
            from agent_bridge.sdk_runner import ClaudeSDKRunner
            runner = ClaudeSDKRunner()

        assert runner._session_id == "session-from-disk"

    def test_no_file_starts_fresh(self, tmp_path):
        """Without a session file, the runner should start with session_id=None."""
        runner = _get_patched_runner(tmp_path)
        assert runner._session_id is None

    def test_corrupt_file_starts_fresh(self, tmp_path):
        """An empty or whitespace-only session file should result in None."""
        session_file = tmp_path / ".agent_session"
        session_file.write_text("   \n  ")

        with patch("agent_bridge.sdk_runner._SESSION_FILE", session_file):
            from agent_bridge.sdk_runner import ClaudeSDKRunner
            runner = ClaudeSDKRunner()

        assert runner._session_id is None

    @pytest.mark.asyncio
    async def test_session_id_persisted_after_query(self, tmp_path):
        """After a full query cycle, the session file should contain the new ID."""
        session_file = tmp_path / ".agent_session"
        runner = _get_patched_runner(tmp_path)

        async def mock_query(prompt, options):
            yield FakeStreamEvent({"type": "message_start"}, session_id="session-live")

        patches = _make_patches(mock_query)
        patches.append(patch("agent_bridge.sdk_runner._SESSION_FILE", session_file))
        for p in patches:
            p.start()
        try:
            await _collect_events(runner, "Hi")
        finally:
            for p in patches:
                p.stop()

        assert session_file.read_text() == "session-live"

    @pytest.mark.asyncio
    async def test_resume_set_on_second_query(self, tmp_path):
        """The second query should set options.resume with the first query's session ID."""
        runner = _get_patched_runner(tmp_path)
        session_file = tmp_path / ".agent_session"

        call_count = 0

        async def mock_query(prompt, options):
            nonlocal call_count
            call_count += 1
            yield FakeStreamEvent({"type": "message_start"}, session_id="session-multi")

        mock_options_cls = MagicMock()
        patches = [
            patch("agent_bridge.sdk_runner.query", side_effect=mock_query),
            patch("agent_bridge.sdk_runner.StreamEvent", FakeStreamEvent),
            patch("agent_bridge.sdk_runner.ClaudeAgentOptions", mock_options_cls),
            patch("agent_bridge.sdk_runner._SESSION_FILE", session_file),
        ]
        for p in patches:
            p.start()
        try:
            # First query — no resume expected.
            await _collect_events(runner, "Hello")
            first_call_kwargs = mock_options_cls.call_args

            # Second query — should set resume on the options object.
            await _collect_events(runner, "Follow up")
        finally:
            for p in patches:
                p.stop()

        assert call_count == 2
        # The options mock's return value should have resume set.
        options_instance = mock_options_cls.return_value
        assert options_instance.resume == "session-multi"


class TestResumeFailureRecovery:
    """Tests for graceful recovery when resume fails with ProcessError."""

    @pytest.mark.asyncio
    async def test_process_error_retries_without_resume(self, tmp_path):
        """ProcessError with active session should retry without resume."""
        runner = _get_patched_runner(tmp_path)
        runner._session_id = "expired-session"
        session_file = tmp_path / ".agent_session"
        session_file.write_text("expired-session")

        call_count = 0

        async def mock_query(prompt, options):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise ProcessError("Session expired")
            # Second call succeeds.
            yield FakeStreamEvent({"type": "message_start"}, session_id="new-session")
            yield FakeStreamEvent({
                "type": "content_block_delta",
                "delta": {"type": "text_delta", "text": "Recovered!"},
            })

        patches = _make_patches(mock_query)
        patches.append(patch("agent_bridge.sdk_runner._SESSION_FILE", session_file))
        for p in patches:
            p.start()
        try:
            events = await _collect_events(runner, "Hi")
        finally:
            for p in patches:
                p.stop()

        assert call_count == 2
        text_events = [e for e in events if e["type"] == "text_delta"]
        assert len(text_events) == 1
        assert text_events[0]["text"] == "Recovered!"

    @pytest.mark.asyncio
    async def test_retry_also_fails_yields_error(self, tmp_path):
        """If retry without resume also fails, an error event should be yielded."""
        runner = _get_patched_runner(tmp_path)
        runner._session_id = "bad-session"
        session_file = tmp_path / ".agent_session"

        async def mock_query(prompt, options):
            raise ProcessError("Always fails")
            yield  # Make it an async generator.

        patches = _make_patches(mock_query)
        patches.append(patch("agent_bridge.sdk_runner._SESSION_FILE", session_file))
        for p in patches:
            p.start()
        try:
            events = await _collect_events(runner, "Hi")
        finally:
            for p in patches:
                p.stop()

        error_events = [e for e in events if e["type"] == "error"]
        assert len(error_events) == 1
        assert "Always fails" in error_events[0]["text"]

    @pytest.mark.asyncio
    async def test_process_error_without_session_yields_error(self, tmp_path):
        """ProcessError without an active session should not retry."""
        runner = _get_patched_runner(tmp_path)
        # No session — so ProcessError is not a resume failure.
        assert runner._session_id is None

        call_count = 0

        async def mock_query(prompt, options):
            nonlocal call_count
            call_count += 1
            raise ProcessError("SDK broken")
            yield  # Make it an async generator.

        patches = _make_patches(mock_query)
        patches.append(patch("agent_bridge.sdk_runner._SESSION_FILE", tmp_path / ".agent_session"))
        for p in patches:
            p.start()
        try:
            events = await _collect_events(runner, "Hi")
        finally:
            for p in patches:
                p.stop()

        # Should NOT retry — only one call.
        assert call_count == 1
        error_events = [e for e in events if e["type"] == "error"]
        assert len(error_events) == 1


class TestConcurrencyLock:
    """Tests for the asyncio.Lock serialization on send_message."""

    @pytest.mark.asyncio
    async def test_messages_are_serialized(self, tmp_path):
        """Concurrent send_message calls should execute one at a time."""
        runner = _get_patched_runner(tmp_path)
        session_file = tmp_path / ".agent_session"

        execution_order = []

        async def mock_query(prompt, options):
            execution_order.append(f"start:{prompt}")
            # Simulate some async work so the second call has a chance to try.
            await asyncio.sleep(0.01)
            execution_order.append(f"end:{prompt}")
            yield FakeStreamEvent({"type": "message_start"}, session_id="s1")

        patches = _make_patches(mock_query)
        patches.append(patch("agent_bridge.sdk_runner._SESSION_FILE", session_file))
        for p in patches:
            p.start()
        try:
            # Launch two messages concurrently.
            task1 = asyncio.create_task(_collect_events(runner, "first"))
            task2 = asyncio.create_task(_collect_events(runner, "second"))
            await asyncio.gather(task1, task2)
        finally:
            for p in patches:
                p.stop()

        # Due to lock, first must fully complete before second starts.
        assert execution_order[0] == "start:first"
        assert execution_order[1] == "end:first"
        assert execution_order[2] == "start:second"
        assert execution_order[3] == "end:second"

    @pytest.mark.asyncio
    async def test_lock_released_after_error(self, tmp_path):
        """The lock should be released even if query raises an exception."""
        runner = _get_patched_runner(tmp_path)
        session_file = tmp_path / ".agent_session"

        call_count = 0

        async def mock_query(prompt, options):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("Boom")
            yield FakeStreamEvent({"type": "message_start"}, session_id="s1")

        patches = _make_patches(mock_query)
        patches.append(patch("agent_bridge.sdk_runner._SESSION_FILE", session_file))
        for p in patches:
            p.start()
        try:
            # First call errors.
            await _collect_events(runner, "fail")
            # Second call should succeed — lock was released.
            events = await _collect_events(runner, "succeed")
        finally:
            for p in patches:
                p.stop()

        # Second call got through.
        result_events = [e for e in events if e["type"] == "result"]
        assert len(result_events) >= 1


class TestClearSession:
    """Tests for the clear_session method."""

    def test_clear_resets_session_id(self, tmp_path):
        """clear_session should set _session_id to None."""
        runner = _get_patched_runner(tmp_path)
        runner._session_id = "some-session"

        with patch("agent_bridge.sdk_runner._SESSION_FILE", tmp_path / ".agent_session"):
            runner.clear_session()

        assert runner._session_id is None

    def test_clear_deletes_session_file(self, tmp_path):
        """clear_session should remove the session file from disk."""
        session_file = tmp_path / ".agent_session"
        session_file.write_text("session-to-delete")
        runner = _get_patched_runner(tmp_path)
        runner._session_id = "session-to-delete"

        with patch("agent_bridge.sdk_runner._SESSION_FILE", session_file):
            runner.clear_session()

        assert not session_file.exists()

    def test_clear_missing_file_no_error(self, tmp_path):
        """clear_session should not raise if the session file doesn't exist."""
        runner = _get_patched_runner(tmp_path)
        runner._session_id = "orphan"

        # File doesn't exist — should not raise.
        with patch("agent_bridge.sdk_runner._SESSION_FILE", tmp_path / ".nonexistent"):
            runner.clear_session()

        assert runner._session_id is None

    def test_get_session_info(self, tmp_path):
        """get_session_info should return current state."""
        runner = _get_patched_runner(tmp_path)
        runner._session_id = "info-session"

        info = runner.get_session_info()
        assert info["session_id"] == "info-session"
        assert info["has_session"] is True

    def test_get_session_info_no_session(self, tmp_path):
        """get_session_info with no session should report has_session=False."""
        runner = _get_patched_runner(tmp_path)

        info = runner.get_session_info()
        assert info["session_id"] is None
        assert info["has_session"] is False
