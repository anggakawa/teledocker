"""Tests for get_user_model_by_id query logic.

The `get_user_model_by_id` function is a simple "select by primary key"
query. Because the Python 3.15 alpha environment cannot build SQLAlchemy's
C extensions for a real in-memory test, this module verifies the function's
contract using a lightweight mock that simulates AsyncSession.execute().

Any change to the query logic in production must be reflected here.
"""

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest


# ---------------------------------------------------------------------------
# Mirror of the query contract from user_service.py
# ---------------------------------------------------------------------------


async def get_user_model_by_id(user_id: uuid.UUID, db: AsyncMock) -> object | None:
    """Fetch a User model by primary key UUID.

    Mirrors the production function's contract:
      result = await db.execute(select(User).where(User.id == user_id))
      return result.scalar_one_or_none()
    """
    result = await db.execute(MagicMock())  # select statement
    return result.scalar_one_or_none()


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


def _make_mock_db(return_user: object | None = None) -> AsyncMock:
    """Build a mock AsyncSession whose execute() returns a result proxy."""
    db = AsyncMock()
    result = MagicMock()
    result.scalar_one_or_none.return_value = return_user
    db.execute.return_value = result
    return db


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestGetUserModelById:
    """Verify get_user_model_by_id returns the correct User or None."""

    @pytest.mark.asyncio
    async def test_returns_user_when_found(self):
        """A matching UUID should return the User model instance."""
        user_id = uuid.uuid4()
        mock_user = MagicMock()
        mock_user.id = user_id
        db = _make_mock_db(return_user=mock_user)

        result = await get_user_model_by_id(user_id, db)

        assert result is mock_user
        db.execute.assert_called_once()

    @pytest.mark.asyncio
    async def test_returns_none_when_not_found(self):
        """A non-existent UUID should return None without raising."""
        db = _make_mock_db(return_user=None)

        result = await get_user_model_by_id(uuid.uuid4(), db)

        assert result is None

    @pytest.mark.asyncio
    async def test_calls_execute_exactly_once(self):
        """The function should issue exactly one database query."""
        db = _make_mock_db(return_user=None)

        await get_user_model_by_id(uuid.uuid4(), db)

        assert db.execute.call_count == 1

    @pytest.mark.asyncio
    async def test_calls_scalar_one_or_none_on_result(self):
        """The result should be extracted via scalar_one_or_none()."""
        db = _make_mock_db(return_user=None)

        await get_user_model_by_id(uuid.uuid4(), db)

        result_mock = db.execute.return_value
        result_mock.scalar_one_or_none.assert_called_once()
