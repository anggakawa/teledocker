"""Tests for session endpoint guards against missing container_id.

When container creation fails, the session is saved with container_id=None.
These tests verify that endpoints reject such sessions early with clear
error messages, rather than proxying to containers/None/... and 500-ing.

The two items under test:

  _NO_CONTAINER_ERROR — the user-facing error message constant.
  _no_container_response() — returns an SSE StreamingResponse with an error
      event followed by [DONE].

Because importing api_server.routers.sessions triggers pydantic-settings
validation (requires DATABASE_URL, ENCRYPTION_KEY_HEX, SERVICE_TOKEN at
module level), we follow the same pattern used throughout this test suite:
mirror the production code exactly and test the mirror. Any change to the
constant or the helper in sessions.py MUST also be reflected here.

Reference: api_server/routers/sessions.py lines 32-42.
"""

import json

import pytest
from fastapi.responses import StreamingResponse


# ---------------------------------------------------------------------------
# Mirror of the constant and helper from api_server/routers/sessions.py
# ---------------------------------------------------------------------------

_NO_CONTAINER_ERROR = "Session has no container — it may have failed to provision."


def _no_container_response() -> StreamingResponse:
    """Return an SSE error stream when the session has no container_id.

    Mirrors the production function from sessions.py exactly. The stream
    yields one data frame containing a JSON error payload, followed by the
    [DONE] sentinel that signals end-of-stream to the SSE client.
    """

    async def error_stream():
        yield f"data: {json.dumps({'error': _NO_CONTAINER_ERROR})}\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(error_stream(), media_type="text/event-stream")


# ---------------------------------------------------------------------------
# Helper: consume an async body iterator into a plain list
# ---------------------------------------------------------------------------


async def _collect_frames(response: StreamingResponse) -> list[str]:
    """Drain a StreamingResponse body iterator and return all yielded chunks.

    StreamingResponse wraps the async generator in response.body_iterator.
    We iterate it here to get the raw string frames for assertion.
    """
    frames: list[str] = []
    async for chunk in response.body_iterator:
        frames.append(chunk)
    return frames


# ---------------------------------------------------------------------------
# Tests: _no_container_response() return value and stream contents
# ---------------------------------------------------------------------------


class TestNoContainerResponse:
    """Verify the SSE error stream returned for sessions without a container."""

    @pytest.mark.asyncio
    async def test_returns_streaming_response(self):
        """The helper returns a FastAPI StreamingResponse object."""
        response = _no_container_response()

        assert isinstance(response, StreamingResponse), (
            "_no_container_response() must return a StreamingResponse, "
            "not a plain dict or HTTPException."
        )

    @pytest.mark.asyncio
    async def test_media_type_is_sse(self):
        """The response must declare text/event-stream as its media type.

        The Telegram bot's SSE client expects this header to correctly parse
        the stream as Server-Sent Events.
        """
        response = _no_container_response()

        assert response.media_type == "text/event-stream", (
            "SSE responses must use 'text/event-stream' so downstream clients "
            "know to parse the body as Server-Sent Events."
        )

    @pytest.mark.asyncio
    async def test_yields_exactly_two_frames(self):
        """The stream yields exactly two frames: one error event and one [DONE].

        Yielding more frames would confuse clients that stop after [DONE].
        Yielding fewer would leave the stream open or missing the error event.
        """
        response = _no_container_response()

        frames = await _collect_frames(response)

        assert len(frames) == 2, (
            f"Expected exactly 2 frames (error + [DONE]), got {len(frames)}: {frames!r}"
        )

    @pytest.mark.asyncio
    async def test_first_frame_starts_with_data_prefix(self):
        """The error frame must start with 'data: ' per the SSE specification."""
        response = _no_container_response()

        frames = await _collect_frames(response)
        first_frame = frames[0]

        assert first_frame.startswith("data: "), (
            f"First frame must begin with 'data: ' (SSE spec), got: {first_frame!r}"
        )

    @pytest.mark.asyncio
    async def test_second_frame_is_done_sentinel(self):
        """The second and final frame must be the [DONE] sentinel.

        The telegram-bot SSE reader polls for this exact string to know the
        stream has ended. Any other value would leave it waiting indefinitely.
        """
        response = _no_container_response()

        frames = await _collect_frames(response)
        second_frame = frames[1]

        assert second_frame == "data: [DONE]\n\n", (
            f"Second frame must be 'data: [DONE]\\n\\n', got: {second_frame!r}"
        )

    @pytest.mark.asyncio
    async def test_error_frame_contains_json_payload(self):
        """The first frame's payload must be valid JSON.

        The telegram-bot parses each SSE payload as JSON to detect error keys.
        A non-JSON payload would be silently ignored or mishandled.
        """
        response = _no_container_response()

        frames = await _collect_frames(response)
        first_frame = frames[0]

        # Strip the 'data: ' prefix and the trailing double newline.
        raw_payload = first_frame.removeprefix("data: ").rstrip("\n")

        # This must not raise — if it does, the frame is not valid JSON.
        try:
            parsed_payload = json.loads(raw_payload)
        except json.JSONDecodeError as exc:
            pytest.fail(
                f"Error frame payload is not valid JSON. "
                f"Payload was: {raw_payload!r}. Error: {exc}"
            )

        assert isinstance(parsed_payload, dict), (
            "Parsed payload must be a JSON object (dict), not a list or scalar."
        )

    @pytest.mark.asyncio
    async def test_error_event_contains_error_key(self):
        """The JSON payload must have an 'error' key for client detection.

        The telegram-bot checks for the 'error' key to distinguish error
        events from normal chunk events.
        """
        response = _no_container_response()

        frames = await _collect_frames(response)
        first_frame = frames[0]

        raw_payload = first_frame.removeprefix("data: ").rstrip("\n")
        parsed_payload = json.loads(raw_payload)

        assert "error" in parsed_payload, (
            f"Error frame must contain an 'error' key. Got keys: {list(parsed_payload.keys())}"
        )

    @pytest.mark.asyncio
    async def test_error_event_contains_descriptive_message(self):
        """The error value must be the exact _NO_CONTAINER_ERROR string.

        The message is user-facing — changing it silently would break
        any client-side string matching or log grep patterns.
        """
        response = _no_container_response()

        frames = await _collect_frames(response)
        first_frame = frames[0]

        raw_payload = first_frame.removeprefix("data: ").rstrip("\n")
        parsed_payload = json.loads(raw_payload)
        error_message = parsed_payload["error"]

        assert error_message == _NO_CONTAINER_ERROR, (
            f"Error message in frame must equal _NO_CONTAINER_ERROR.\n"
            f"Expected: {_NO_CONTAINER_ERROR!r}\n"
            f"Got:      {error_message!r}"
        )

    @pytest.mark.asyncio
    async def test_first_frame_ends_with_double_newline(self):
        """Every SSE frame must end with '\\n\\n' per the SSE specification.

        A missing double newline would cause the SSE parser to merge frames
        or hold them in a buffer until a timeout.
        """
        response = _no_container_response()

        frames = await _collect_frames(response)
        first_frame = frames[0]

        assert first_frame.endswith("\n\n"), (
            f"SSE frames must end with '\\n\\n'. First frame was: {first_frame!r}"
        )

    @pytest.mark.asyncio
    async def test_first_frame_has_no_double_data_prefix(self):
        """The error frame must not have a doubled 'data: data: ' prefix.

        This double-prefix bug was fixed in the exec/message generate()
        closures (see test_sse_proxy.py). We verify the helper does not
        re-introduce the same issue.
        """
        response = _no_container_response()

        frames = await _collect_frames(response)
        first_frame = frames[0]

        assert not first_frame.startswith("data: data: "), (
            f"Frame must not have a double 'data: data: ' prefix. "
            f"Got: {first_frame!r}"
        )

    @pytest.mark.asyncio
    async def test_each_call_produces_an_independent_stream(self):
        """Two independent calls each produce their own fresh generator.

        A reused or exhausted generator would yield nothing on the second call,
        which would leave clients hanging with no frames.
        """
        response_a = _no_container_response()
        response_b = _no_container_response()

        frames_a = await _collect_frames(response_a)
        frames_b = await _collect_frames(response_b)

        assert len(frames_a) == 2, "First response must yield 2 frames."
        assert len(frames_b) == 2, "Second response must yield 2 frames independently."
        assert frames_a == frames_b, (
            "Both responses must produce identical frames since the error is constant."
        )


# ---------------------------------------------------------------------------
# Tests: _NO_CONTAINER_ERROR constant
# ---------------------------------------------------------------------------


class TestNoContainerErrorConstant:
    """Verify the error message string is correct, user-facing, and safe."""

    def test_error_message_is_a_non_empty_string(self):
        """The constant must be a non-empty string — not None, not empty."""
        assert isinstance(_NO_CONTAINER_ERROR, str), (
            "_NO_CONTAINER_ERROR must be a str."
        )
        assert len(_NO_CONTAINER_ERROR) > 0, (
            "_NO_CONTAINER_ERROR must not be an empty string."
        )

    def test_error_message_mentions_container(self):
        """The message must reference containers so the user knows what failed."""
        message_lower = _NO_CONTAINER_ERROR.lower()

        assert "container" in message_lower, (
            "The error message must mention 'container' so the user "
            "understands which resource is missing."
        )

    def test_error_message_mentions_provision_failure(self):
        """The message must indicate a provisioning failure as the likely cause."""
        message_lower = _NO_CONTAINER_ERROR.lower()

        mentions_provision = "provision" in message_lower
        mentions_no_container = "no container" in message_lower

        assert mentions_provision or mentions_no_container, (
            "The error message must tell the user what likely went wrong "
            "(e.g., 'failed to provision' or 'no container')."
        )

    def test_error_message_is_json_safe(self):
        """The message must survive a JSON round-trip without corruption.

        json.dumps() and json.loads() must reproduce the exact same string.
        Control characters or unescaped quotes would cause JSON parse errors
        on the client side.
        """
        serialised = json.dumps({"error": _NO_CONTAINER_ERROR})

        parsed = json.loads(serialised)
        recovered_message = parsed["error"]

        assert recovered_message == _NO_CONTAINER_ERROR, (
            f"Message did not survive JSON round-trip.\n"
            f"Original:  {_NO_CONTAINER_ERROR!r}\n"
            f"Recovered: {recovered_message!r}"
        )

    def test_error_message_contains_no_trailing_whitespace(self):
        """The message must not have leading or trailing whitespace.

        Extra whitespace would be visible in the Telegram chat as odd spacing
        and could confuse string-matching in client code.
        """
        assert _NO_CONTAINER_ERROR == _NO_CONTAINER_ERROR.strip(), (
            f"Error message must not have leading or trailing whitespace. "
            f"Got: {_NO_CONTAINER_ERROR!r}"
        )
