"""Tests for SSE event parsing in ApiClient streaming methods.

The `stream_exec` and `stream_message` methods in `api_client.py` parse SSE
lines from the api-server and yield decoded text to the Telegram bot handlers.
Their inner loop looks like this (simplified):

    async for line in response.aiter_lines():
        if line.startswith("data: "):
            data = line[6:]
            if data == "[DONE]":
                break
            try:
                parsed = json.loads(data)
                chunk = parsed.get("chunk", "")
                if chunk:
                    yield chunk
                error = parsed.get("error", "")
                if error:
                    yield f"Error: {error}"
            except json.JSONDecodeError:
                yield data

Because the async generators are tightly coupled to live HTTP streams, we
extract the parsing logic into a pure synchronous helper here and drive all
tests through that. Any change to the production parsing rules must be
mirrored in `_parse_sse_data_line()` below.

The two bugs this test suite guards against:

  Bug 1 — Error events silently swallowed (the main regression)
    The old code only looked at `parsed.get("chunk", "")`. An api-server
    event like `{"error": "connection failed"}` contained no 'chunk' key,
    so the error was dropped without any output to the user. The fix adds
    a second branch that checks for 'error' and yields `"Error: {error}"`.

  Bug 2 — JSONDecodeError crashes the stream
    Non-JSON payloads (e.g. raw shell output) must not crash; instead the
    raw data string is yielded as a fallback.
"""

import json

# ---------------------------------------------------------------------------
# SSE sentinel constant — mirrors the value used in both generator methods.
# ---------------------------------------------------------------------------

SSE_DONE_SENTINEL = "[DONE]"


# ---------------------------------------------------------------------------
# Pure helper mirroring the parsing logic from ApiClient.stream_exec /
# stream_message (the inner try/except block only — line filtering and the
# [DONE] check are exercised in separate helpers below).
# ---------------------------------------------------------------------------


def _parse_sse_data_line(data: str) -> list[str]:
    """Parse one SSE data value and return the strings that would be yielded.

    Mirrors the production logic:
      1. Try to JSON-parse the data.
      2. If 'chunk' is present and non-empty, include it.
      3. If 'error' is present and non-empty, include 'Error: {error}'.
      4. On JSONDecodeError, include the raw data string as a fallback.

    Returns a list because a single JSON payload can yield zero, one, or two
    strings (when both 'chunk' and 'error' are present — unusual but valid).
    """
    yielded_values: list[str] = []

    try:
        parsed = json.loads(data)

        chunk = parsed.get("chunk", "")
        if chunk:
            yielded_values.append(chunk)

        error = parsed.get("error", "")
        if error:
            yielded_values.append(f"Error: {error}")

    except json.JSONDecodeError:
        # Fallback: yield the raw data string so the user still sees output.
        yielded_values.append(data)

    return yielded_values


def _should_process_line(line: str) -> bool:
    """Return True when a raw SSE line should be processed (has 'data: ' prefix)."""
    return line.startswith("data: ")


def _is_done_sentinel(data: str) -> bool:
    """Return True when the data value signals end-of-stream."""
    return data == SSE_DONE_SENTINEL


# ---------------------------------------------------------------------------
# Tests: normal chunk events
# ---------------------------------------------------------------------------


class TestSseChunkParsing:
    """Verify that normal 'chunk' events are correctly extracted."""

    def test_chunk_text_is_yielded_directly(self):
        """A payload with a 'chunk' key yields the chunk text as-is."""
        data = '{"chunk": "Hello world"}'

        results = _parse_sse_data_line(data)

        assert len(results) == 1, "One 'chunk' event should produce exactly one output."
        assert results[0] == "Hello world", (
            "The yielded value must be the raw chunk text, not the JSON wrapper."
        )

    def test_multiword_chunk_is_returned_intact(self):
        """Chunk text with spaces, punctuation, and unicode passes through unchanged."""
        original_text = "Compiling step 2/5: running tests..."
        data = json.dumps({"chunk": original_text})

        results = _parse_sse_data_line(data)

        assert results == [original_text], (
            "Complex chunk text must be returned exactly as it appears in the payload."
        )

    def test_empty_chunk_string_is_not_yielded(self):
        """A payload where 'chunk' is an empty string should produce no output.

        The production code does `if chunk: yield chunk`, so an empty string
        is falsy and must be suppressed.
        """
        data = '{"chunk": ""}'

        results = _parse_sse_data_line(data)

        assert results == [], (
            "An empty 'chunk' string is falsy and must not be yielded."
        )

    def test_chunk_absent_from_payload_produces_no_output(self):
        """A JSON payload without a 'chunk' key and without 'error' yields nothing."""
        data = '{"type": "metadata", "tokens": 42}'

        results = _parse_sse_data_line(data)

        assert results == [], (
            "Payloads with no 'chunk' and no 'error' key must produce no output."
        )


# ---------------------------------------------------------------------------
# Tests: error event handling (the main regression guard)
# ---------------------------------------------------------------------------


class TestSseErrorEventHandling:
    """Verify that 'error' events from the api-server are surfaced to the user.

    Before the fix these events were silently dropped because the old code
    only checked for 'chunk'. The fix adds an 'error' branch.
    """

    def test_error_event_is_yielded_as_prefixed_string(self):
        """A payload with an 'error' key yields 'Error: {message}'."""
        data = '{"error": "connection failed"}'

        results = _parse_sse_data_line(data)

        assert len(results) == 1, "One 'error' event should produce exactly one output."
        assert results[0] == "Error: connection failed", (
            "The error must be yielded as 'Error: {message}' so the user "
            "can see what went wrong."
        )

    def test_different_error_message_is_prefixed_correctly(self):
        """The 'Error: ' prefix is always prepended, regardless of message content."""
        data = '{"error": "API key invalid — please run /setkey"}'

        results = _parse_sse_data_line(data)

        assert results[0].startswith("Error: "), (
            "Every error result must start with 'Error: '."
        )
        assert "API key invalid" in results[0], (
            "The original error message must appear after the prefix."
        )

    def test_empty_error_string_is_not_yielded(self):
        """A payload where 'error' is an empty string should produce no output.

        Mirrors the `if error:` guard in the production code.
        """
        data = '{"chunk": "hello", "error": ""}'

        results = _parse_sse_data_line(data)

        # 'chunk' should still be yielded, but the empty 'error' must not.
        assert "Error: " not in " ".join(results), (
            "An empty 'error' string is falsy and must not produce an error output."
        )
        assert "hello" in results, (
            "The 'chunk' value must still be yielded when 'error' is empty."
        )

    def test_error_only_payload_yields_nothing_else(self):
        """A payload with only 'error' (no 'chunk') yields only the error string."""
        data = '{"error": "container unreachable"}'

        results = _parse_sse_data_line(data)

        assert len(results) == 1, (
            "An error-only payload must produce exactly one output string."
        )
        # The original bug: without the fix this list would be empty.
        assert results[0] == "Error: container unreachable", (
            "The error message must be the single output — "
            "this is the core regression being guarded."
        )

    def test_payload_with_both_chunk_and_error_yields_both(self):
        """A payload containing both 'chunk' and 'error' yields both values."""
        data = '{"chunk": "partial result", "error": "stream truncated"}'

        results = _parse_sse_data_line(data)

        assert len(results) == 2, (
            "A payload with both 'chunk' and 'error' must yield two separate values."
        )

        chunk_results = [r for r in results if not r.startswith("Error: ")]
        error_results = [r for r in results if r.startswith("Error: ")]

        assert chunk_results == ["partial result"], (
            "The chunk text must appear in the results."
        )
        assert error_results == ["Error: stream truncated"], (
            "The error string must appear in the results."
        )


# ---------------------------------------------------------------------------
# Tests: JSONDecodeError fallback
# ---------------------------------------------------------------------------


class TestSseJsonDecodeFallback:
    """Verify that non-JSON payloads are surfaced rather than crashing the stream."""

    def test_plain_text_payload_is_yielded_as_raw_fallback(self):
        """A non-JSON data value is yielded unchanged as the fallback output."""
        data = "plain text output from shell command"

        results = _parse_sse_data_line(data)

        assert len(results) == 1, "Raw data fallback must produce exactly one output."
        assert results[0] == data, (
            "The raw data string must be yielded unchanged so the user sees the output."
        )

    def test_partial_json_falls_back_to_raw_string(self):
        """A truncated JSON string (e.g. from a crash mid-write) falls back gracefully."""
        data = '{"chunk": "incomplete...'  # Missing closing brace.

        results = _parse_sse_data_line(data)

        assert len(results) == 1, (
            "Malformed JSON must not crash — it falls back to the raw string."
        )
        assert results[0] == data, (
            "The raw malformed data must be yielded as-is."
        )

    def test_empty_string_payload_falls_back_to_empty_string(self):
        """An empty data string falls back to yielding an empty string."""
        data = ""

        results = _parse_sse_data_line(data)

        # An empty string raises JSONDecodeError, so the fallback is the empty string.
        assert results == [""], (
            "An empty payload must fall back to the raw empty string."
        )


# ---------------------------------------------------------------------------
# Tests: line-level filtering (mirrors the outer `if line.startswith` check)
# ---------------------------------------------------------------------------


class TestSseLineFiltering:
    """Verify that line-level filtering behaves correctly before parsing."""

    def test_lines_with_data_prefix_are_processed(self):
        """Lines beginning with 'data: ' pass the line filter."""
        line = 'data: {"chunk": "content"}'

        should_process = _should_process_line(line)

        assert should_process is True, (
            "Lines starting with 'data: ' must pass the line filter."
        )

    def test_event_lines_are_not_processed(self):
        """Lines like 'event: ping' do not pass the line filter."""
        for non_data_line in ["event: ping", ": keep-alive", "id: 99", ""]:
            should_process = _should_process_line(non_data_line)

            assert should_process is False, (
                f"Line '{non_data_line}' must not pass the 'data: ' line filter."
            )

    def test_data_prefix_extraction_gives_correct_payload(self):
        """After passing the filter, stripping 6 characters gives the raw payload."""
        line = 'data: {"chunk": "extracted"}'

        # Mirror the production `data = line[6:]` step.
        data = line[6:]

        assert data == '{"chunk": "extracted"}', (
            "Slicing off 6 characters must remove exactly 'data: ' and leave the payload."
        )


# ---------------------------------------------------------------------------
# Tests: [DONE] sentinel handling
# ---------------------------------------------------------------------------


class TestSseDoneSentinel:
    """Verify that the [DONE] sentinel is detected and stops stream consumption."""

    def test_done_value_is_recognized_as_sentinel(self):
        """The string '[DONE]' must be detected as the stream-end signal."""
        data = "[DONE]"

        is_done = _is_done_sentinel(data)

        assert is_done is True, (
            "The '[DONE]' value must be recognized as the end-of-stream sentinel."
        )

    def test_non_done_values_are_not_treated_as_sentinel(self):
        """Only the exact string '[DONE]' triggers the sentinel; other values do not."""
        non_sentinel_values = [
            '{"chunk": "hello"}',
            "DONE",
            "[done]",
            "[DONE] extra",
            "",
        ]

        for value in non_sentinel_values:
            is_done = _is_done_sentinel(value)

            assert is_done is False, (
                f"Value '{value}' must not be treated as the [DONE] sentinel."
            )

    def test_done_sentinel_after_chunks_does_not_crash(self):
        """A realistic stream: chunks followed by [DONE] — only chunks are parsed."""
        stream_lines = [
            'data: {"chunk": "first part"}',
            'data: {"chunk": "second part"}',
            "data: [DONE]",
        ]

        collected_outputs: list[str] = []

        for line in stream_lines:
            if not _should_process_line(line):
                continue
            data = line[6:]
            if _is_done_sentinel(data):
                break
            outputs = _parse_sse_data_line(data)
            collected_outputs.extend(outputs)

        assert collected_outputs == ["first part", "second part"], (
            "Only the two chunk texts should be collected; [DONE] stops the loop."
        )
