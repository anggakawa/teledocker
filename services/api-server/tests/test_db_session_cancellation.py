"""Tests for BaseException handling in get_db_session.

CancelledError (a BaseException in Python 3.9+) must trigger a rollback
just like any regular Exception. Before the fix, `except Exception` would
miss CancelledError entirely, leaving the asyncpg connection in a dirty
state and poisoning the connection pool.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.fixture
def mock_session():
    """Create a mock AsyncSession with rollback tracking."""
    session = AsyncMock()
    session.rollback = AsyncMock()
    return session


@pytest.fixture
def mock_factory(mock_session):
    """Create a mock session factory that returns our mock session.

    The factory returns an async context manager wrapping mock_session.
    This mirrors the real async_sessionmaker().__aenter__/__aexit__ behavior.
    """
    context_manager = AsyncMock()
    context_manager.__aenter__ = AsyncMock(return_value=mock_session)
    context_manager.__aexit__ = AsyncMock(return_value=False)

    factory = MagicMock(return_value=context_manager)
    return factory


class TestGetDbSessionBaseException:
    """Verify that get_db_session rolls back on BaseException subclasses."""

    @pytest.mark.asyncio
    async def test_cancelled_error_triggers_rollback(self, mock_factory, mock_session):
        """CancelledError must trigger rollback, not slip through silently."""
        with patch(
            "api_server.db.engine.get_session_factory", return_value=mock_factory
        ):
            from api_server.db.engine import get_db_session

            with pytest.raises(asyncio.CancelledError):
                async with get_db_session() as _session:
                    raise asyncio.CancelledError()

            mock_session.rollback.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_keyboard_interrupt_triggers_rollback(self, mock_factory, mock_session):
        """KeyboardInterrupt (another BaseException) must also trigger rollback."""
        with patch(
            "api_server.db.engine.get_session_factory", return_value=mock_factory
        ):
            from api_server.db.engine import get_db_session

            with pytest.raises(KeyboardInterrupt):
                async with get_db_session() as _session:
                    raise KeyboardInterrupt()

            mock_session.rollback.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_regular_exception_still_triggers_rollback(
        self, mock_factory, mock_session
    ):
        """Normal exceptions (ValueError, etc.) must continue to trigger rollback."""
        with patch(
            "api_server.db.engine.get_session_factory", return_value=mock_factory
        ):
            from api_server.db.engine import get_db_session

            with pytest.raises(ValueError, match="test error"):
                async with get_db_session() as _session:
                    raise ValueError("test error")

            mock_session.rollback.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_no_error_means_no_rollback(self, mock_factory, mock_session):
        """When the block completes normally, rollback should NOT be called."""
        with patch(
            "api_server.db.engine.get_session_factory", return_value=mock_factory
        ):
            from api_server.db.engine import get_db_session

            async with get_db_session() as _session:
                pass  # No error

            mock_session.rollback.assert_not_awaited()
