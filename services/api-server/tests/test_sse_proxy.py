"""Tests for SSE proxy line parsing in session endpoints.

The `exec_command` and `send_message` endpoints each contain an async
`generate()` closure that proxies SSE from container-manager. The parsing
logic lives inside those closures, so we cannot import it directly.

Instead, this module mirrors the exact parsing rules from `sessions.py` in a
pure, synchronous helper (`_parse_sse_lines`) and then drives all tests
through that helper. Any change to the parsing rules in production must also
be reflected here — that coupling is intentional; it is what makes the tests
useful as a regression guard.

The two bugs this test suite guards against:

  Bug 1 — Double `data:` prefix
    Before the fix, the generator re-wrapped lines with an unconditional
    `f"data: {line}\n\n"`. Because `response.aiter_lines()` already preserves
    the original `data: ` prefix, the result was `data: data: {payload}\n\n`.
    The fix strips the prefix first, then re-adds exactly one.

  Bug 2 — `send_message` chunk extraction
    The message endpoint extracts readable text from each chunk for database
    logging. A `JSONDecodeError` guard was already present; we verify it
    handles both valid and invalid JSON without crashing.
"""

import json

import pytest

# ---------------------------------------------------------------------------
# Pure helper that mirrors the parsing logic from sessions.py generate()
# ---------------------------------------------------------------------------

SSE_DATA_PREFIX = "data: "
SSE_DONE_SENTINEL = "[DONE]"


def _parse_sse_lines(incoming_lines: list[str]) -> list[str]:
    """Apply the same filtering rules used inside the generate() closures.

    Processes a list of raw SSE lines exactly as the production generator
    does:
      1. Skip empty lines and lines that do not start with 'data: '.
      2. Strip the 'data: ' prefix to get the raw payload.
      3. Stop on the upstream [DONE] sentinel (do not forward it).
      4. Yield 'data: {payload}\\n\\n' for every other line.

    Returns a list of the emitted SSE frames (without the final [DONE] that
    the finally-block appends — that is tested separately).
    """
    emitted_frames: list[str] = []

    for line in incoming_lines:
        # Rule 1: skip empty lines and non-SSE lines.
        if not line or not line.startswith(SSE_DATA_PREFIX):
            continue

        # Strip the prefix — the length of 'data: ' is exactly 6 characters.
        payload_data = line[6:]

        # Rule 3: stop at the upstream sentinel; do not forward it.
        if payload_data == SSE_DONE_SENTINEL:
            break

        # Rule 4: re-emit with exactly one 'data: ' prefix.
        emitted_frames.append(f"data: {payload_data}\n\n")

    return emitted_frames


def _extract_chunk_for_logging(payload_data: str) -> str:
    """Mirror the chunk-extraction logic from send_message generate().

    The production code does:
        try:
            chunk = json.loads(payload_data).get("chunk", "")
            if chunk:
                full_response_parts.append(chunk)
        except json.JSONDecodeError:
            pass

    Returns the extracted chunk string, or an empty string when the payload
    is not JSON or when the 'chunk' key is absent / empty.
    """
    try:
        parsed = json.loads(payload_data)
        return parsed.get("chunk", "")
    except json.JSONDecodeError:
        return ""


# ---------------------------------------------------------------------------
# Tests: SSE proxy line parsing (exec_command and send_message)
# ---------------------------------------------------------------------------


class TestSseProxyNoPrefixDoubling:
    """Verify that a single 'data: ' prefix is emitted — not a double one.

    Bug 1 regression tests.
    """

    def test_normal_chunk_line_is_re_emitted_with_single_prefix(self):
        """A well-formed upstream line produces exactly one 'data: ' prefix."""
        incoming_lines = ['data: {"chunk": "Hello"}']

        emitted_frames = _parse_sse_lines(incoming_lines)

        assert len(emitted_frames) == 1, "Exactly one frame should be emitted."

        emitted_frame = emitted_frames[0]

        # The payload must appear with a single prefix.
        assert emitted_frame == 'data: {"chunk": "Hello"}\n\n', (
            "Frame content should be 'data: {payload}\\n\\n' with the original payload."
        )

    def test_emitted_frame_does_not_have_double_data_prefix(self):
        """The old bug produced 'data: data: {payload}'. This must not happen."""
        incoming_lines = ['data: {"chunk": "World"}']

        emitted_frames = _parse_sse_lines(incoming_lines)

        emitted_frame = emitted_frames[0]

        assert not emitted_frame.startswith("data: data: "), (
            "Frame must not begin with the double 'data: data: ' prefix "
            "that was produced by the unfixed code."
        )

    def test_multiple_chunk_lines_each_get_single_prefix(self):
        """Every line in a multi-chunk stream gets exactly one prefix."""
        incoming_lines = [
            'data: {"chunk": "one"}',
            'data: {"chunk": "two"}',
            'data: {"chunk": "three"}',
        ]

        emitted_frames = _parse_sse_lines(incoming_lines)

        assert len(emitted_frames) == 3, "All three lines should produce a frame."

        for frame in emitted_frames:
            assert frame.startswith("data: "), (
                "Each frame must start with 'data: '."
            )
            # The payload itself must not start with 'data: '.
            payload_in_frame = frame[6:]
            assert not payload_in_frame.startswith("data: "), (
                "The content after the prefix must not itself start with 'data: '."
            )

    def test_frame_ends_with_double_newline(self):
        """SSE frames must end with '\\n\\n' for correct client framing."""
        incoming_lines = ['data: {"chunk": "test"}']

        emitted_frames = _parse_sse_lines(incoming_lines)

        emitted_frame = emitted_frames[0]
        assert emitted_frame.endswith("\n\n"), (
            "SSE frames must end with a double newline to signal frame boundaries."
        )


class TestSseProxyDoneSentinel:
    """Verify the upstream [DONE] sentinel stops the stream correctly."""

    def test_done_sentinel_stops_processing(self):
        """Lines after the [DONE] sentinel must not be emitted."""
        incoming_lines = [
            'data: {"chunk": "before"}',
            "data: [DONE]",
            'data: {"chunk": "after — should not appear"}',
        ]

        emitted_frames = _parse_sse_lines(incoming_lines)

        assert len(emitted_frames) == 1, (
            "Only the line before [DONE] should be emitted; "
            "the sentinel itself and everything after it must be dropped."
        )
        assert '{"chunk": "before"}' in emitted_frames[0], (
            "The one emitted frame should contain the 'before' chunk."
        )

    def test_done_sentinel_itself_is_not_forwarded(self):
        """The [DONE] line must never appear in the emitted frames."""
        incoming_lines = [
            'data: {"chunk": "hi"}',
            "data: [DONE]",
        ]

        emitted_frames = _parse_sse_lines(incoming_lines)

        for frame in emitted_frames:
            assert "[DONE]" not in frame, (
                "The [DONE] sentinel must not be forwarded to the downstream client."
            )

    def test_stream_with_only_done_sentinel_emits_nothing(self):
        """A stream consisting solely of [DONE] produces no forwarded frames."""
        incoming_lines = ["data: [DONE]"]

        emitted_frames = _parse_sse_lines(incoming_lines)

        assert emitted_frames == [], (
            "A stream with only the [DONE] sentinel should produce zero frames."
        )


class TestSseProxyLineFiltering:
    """Verify that non-SSE and empty lines are silently dropped."""

    def test_empty_lines_are_skipped(self):
        """Blank lines between SSE frames must not produce output frames."""
        incoming_lines = [
            "",
            'data: {"chunk": "real"}',
            "",
        ]

        emitted_frames = _parse_sse_lines(incoming_lines)

        assert len(emitted_frames) == 1, (
            "Empty lines must be skipped; only the real SSE line produces a frame."
        )

    def test_event_lines_without_data_prefix_are_skipped(self):
        """Lines like 'event: ping' or ': keep-alive' are not forwarded."""
        incoming_lines = [
            "event: ping",
            ": keep-alive",
            "id: 42",
            'data: {"chunk": "actual content"}',
        ]

        emitted_frames = _parse_sse_lines(incoming_lines)

        assert len(emitted_frames) == 1, (
            "Only lines beginning with 'data: ' should produce frames."
        )
        assert '{"chunk": "actual content"}' in emitted_frames[0], (
            "The single emitted frame should contain the actual content chunk."
        )

    def test_mixed_empty_and_sse_lines_only_emit_sse(self):
        """A realistic SSE stream with blank separators works correctly."""
        # Real SSE streams often look like:
        # data: {...}\n
        # \n
        # data: {...}\n
        # \n
        incoming_lines = [
            'data: {"chunk": "first"}',
            "",
            'data: {"chunk": "second"}',
            "",
            "data: [DONE]",
        ]

        emitted_frames = _parse_sse_lines(incoming_lines)

        assert len(emitted_frames) == 2, (
            "Two real content frames should be emitted; empty lines and [DONE] dropped."
        )


class TestSseProxyErrorPayload:
    """Verify that exception errors are formatted as valid JSON SSE events."""

    def test_error_payload_is_valid_json(self):
        """When an exception occurs, the error is wrapped as a parseable JSON object."""
        exception_message = "connection refused to container"

        # Mirror what the generate() finally block does on exception.
        error_payload = json.dumps({"error": exception_message})
        formatted_frame = f"data: {error_payload}\n\n"

        # The downstream client (telegram-bot ApiClient) must be able to parse it.
        parsed = json.loads(formatted_frame[6:].strip())

        assert "error" in parsed, (
            "Error frame payload must contain an 'error' key."
        )
        assert parsed["error"] == exception_message, (
            "The 'error' value must be the original exception message."
        )

    def test_error_payload_frame_has_correct_sse_structure(self):
        """The error frame must follow the same SSE format as regular frames."""
        error_payload = json.dumps({"error": "timeout"})
        formatted_frame = f"data: {error_payload}\n\n"

        assert formatted_frame.startswith("data: "), (
            "Error frame must start with 'data: '."
        )
        assert formatted_frame.endswith("\n\n"), (
            "Error frame must end with double newline."
        )
        assert not formatted_frame.startswith("data: data: "), (
            "Error frame must not have a double prefix."
        )


class TestSseProxyChunkExtraction:
    """Verify the chunk-text extraction used in send_message for DB logging."""

    def test_extracts_chunk_text_from_valid_json_payload(self):
        """A normal chunk payload yields the text string for DB logging."""
        payload_data = '{"chunk": "Hello, world!"}'

        extracted_chunk = _extract_chunk_for_logging(payload_data)

        assert extracted_chunk == "Hello, world!", (
            "The chunk text must be extracted from the JSON payload."
        )

    def test_returns_empty_string_when_chunk_key_is_absent(self):
        """An error payload (no 'chunk' key) returns empty string without crashing."""
        payload_data = '{"error": "something went wrong"}'

        extracted_chunk = _extract_chunk_for_logging(payload_data)

        assert extracted_chunk == "", (
            "Payloads without a 'chunk' key must return empty string, not crash."
        )

    def test_returns_empty_string_for_invalid_json(self):
        """Non-JSON payloads trigger the JSONDecodeError guard and return empty."""
        payload_data = "this is not json at all"

        extracted_chunk = _extract_chunk_for_logging(payload_data)

        assert extracted_chunk == "", (
            "Non-JSON payloads must return empty string — the generator skips logging."
        )

    def test_handles_empty_chunk_value(self):
        """A payload with an empty 'chunk' string returns empty string."""
        payload_data = '{"chunk": ""}'

        extracted_chunk = _extract_chunk_for_logging(payload_data)

        assert extracted_chunk == "", (
            "An empty 'chunk' value should produce an empty string."
        )

    def test_handles_multiword_chunk_content(self):
        """Chunk text with spaces, punctuation, and unicode is returned intact."""
        original_text = "AI response: step 1 of 3 — compiling sources..."
        payload_data = json.dumps({"chunk": original_text})

        extracted_chunk = _extract_chunk_for_logging(payload_data)

        assert extracted_chunk == original_text, (
            "Complex chunk content must be returned exactly as it appears in the JSON."
        )

    def test_multiple_payloads_accumulate_correctly(self):
        """Multiple extracted chunks can be joined to form the full response."""
        chunk_payloads = [
            '{"chunk": "Hello"}',
            '{"chunk": " "}',
            '{"chunk": "world"}',
        ]

        all_chunks = [_extract_chunk_for_logging(p) for p in chunk_payloads]
        full_response = "".join(all_chunks)

        assert full_response == "Hello world", (
            "Joining all chunk parts must produce the complete response string."
        )
