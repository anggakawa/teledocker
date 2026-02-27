"""Tests for the fire-and-forget _log_outbound_message helper.

The helper runs outbound message logging in a separate asyncio task,
decoupled from the SSE generator's cancellation scope. It must:
  1. Successfully log messages via a fresh DB session.
  2. Swallow DB errors gracefully (log them, but never propagate).

Because importing the sessions router triggers pydantic-settings validation
(requires DATABASE_URL, ENCRYPTION_KEY_HEX env vars), these tests mirror the
function logic — the same approach used by test_build_env_vars.py and
test_sse_proxy.py.
"""

import logging
import uuid
from unittest.mock import AsyncMock

import pytest

# ---------------------------------------------------------------------------
# Mirror of _log_outbound_message from api_server/routers/sessions.py
# ---------------------------------------------------------------------------

logger = logging.getLogger("api_server.routers.sessions")


async def _log_outbound_message(
    session_id: uuid.UUID,
    full_response: str,
    elapsed_ms: int,
    # Test injection points (production code uses module-level imports).
    get_db_session_fn=None,
    log_message_fn=None,
) -> None:
    """Log an outbound AI response in a fire-and-forget task.

    Mirrors the production function, but accepts injectable dependencies
    for testing without needing real DB connections.
    """
    try:
        async with get_db_session_fn() as db:
            await log_message_fn(
                session_id=session_id,
                direction="outbound",
                content_type="text",
                content=full_response,
                db=db,
                processing_ms=elapsed_ms,
            )
    except Exception:
        logger.exception(
            "Failed to log outbound message for session %s", session_id
        )


# ---------------------------------------------------------------------------
# Test fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def session_id():
    return uuid.uuid4()


@pytest.fixture
def mock_db_session():
    """Create a mock async context manager that yields a mock DB session."""
    mock_db = AsyncMock()

    class _MockCtx:
        async def __aenter__(self):
            return mock_db

        async def __aexit__(self, *args):
            pass

    return _MockCtx, mock_db


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestLogOutboundMessage:
    """Verify _log_outbound_message handles success and failure correctly."""

    @pytest.mark.asyncio
    async def test_logs_message_successfully(self, session_id, mock_db_session):
        """Happy path: log_message is called with correct args."""
        ctx_cls, mock_db = mock_db_session
        mock_log_message = AsyncMock()

        await _log_outbound_message(
            session_id=session_id,
            full_response="Hello from Claude",
            elapsed_ms=1500,
            get_db_session_fn=ctx_cls,
            log_message_fn=mock_log_message,
        )

        mock_log_message.assert_awaited_once_with(
            session_id=session_id,
            direction="outbound",
            content_type="text",
            content="Hello from Claude",
            db=mock_db,
            processing_ms=1500,
        )

    @pytest.mark.asyncio
    async def test_db_error_is_swallowed(self, session_id, mock_db_session):
        """DB failures must be logged but never propagated to the caller."""
        ctx_cls, _mock_db = mock_db_session
        mock_log_message = AsyncMock(side_effect=RuntimeError("DB connection lost"))

        # Must NOT raise — the error should be caught and logged.
        await _log_outbound_message(
            session_id=session_id,
            full_response="Some response",
            elapsed_ms=500,
            get_db_session_fn=ctx_cls,
            log_message_fn=mock_log_message,
        )

    @pytest.mark.asyncio
    async def test_db_error_is_logged(self, session_id, mock_db_session, caplog):
        """DB failures should produce a log message at ERROR level."""
        ctx_cls, _mock_db = mock_db_session
        mock_log_message = AsyncMock(side_effect=RuntimeError("connection refused"))

        with caplog.at_level(logging.ERROR, logger="api_server.routers.sessions"):
            await _log_outbound_message(
                session_id=session_id,
                full_response="Some response",
                elapsed_ms=500,
                get_db_session_fn=ctx_cls,
                log_message_fn=mock_log_message,
            )

        assert "Failed to log outbound message" in caplog.text

    @pytest.mark.asyncio
    async def test_empty_response_is_logged(self, session_id, mock_db_session):
        """An empty response (zero chunks received) should still be logged."""
        ctx_cls, _mock_db = mock_db_session
        mock_log_message = AsyncMock()

        await _log_outbound_message(
            session_id=session_id,
            full_response="",
            elapsed_ms=200,
            get_db_session_fn=ctx_cls,
            log_message_fn=mock_log_message,
        )

        mock_log_message.assert_awaited_once()
        call_kwargs = mock_log_message.call_args.kwargs
        assert call_kwargs["content"] == ""
        assert call_kwargs["processing_ms"] == 200
