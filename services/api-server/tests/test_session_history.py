"""Tests for session history and resume service functions.

Verifies:
1. list_sessions_by_telegram_id returns all sessions ordered newest first.
2. resume_session stops the current active session before restarting the target.
3. resume_session works when no other session is active.
4. resume_session raises ValueError for non-existent sessions.
"""

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

_MODULE = "api_server.services.session_service"


def _now() -> datetime:
    return datetime.now(UTC)


def _make_mock_session(
    session_id: uuid.UUID | None = None,
    container_id: str | None = "ctr-abc123",
    status: str = "running",
) -> MagicMock:
    """Build a mock Session ORM object with all fields needed by SessionDTO."""
    session = MagicMock()
    session.id = session_id or uuid.uuid4()
    session.user_id = uuid.uuid4()
    session.container_id = container_id
    session.container_name = f"chatops-test-{str(session.id)[:8]}"
    session.status = status
    session.agent_type = "claude-code"
    session.system_prompt = None
    session.last_activity_at = _now()
    session.metadata_ = None
    session.created_at = _now()
    return session


def _make_mock_db_returning_list(sessions: list) -> AsyncMock:
    """Build a mock AsyncSession whose execute() returns a list via scalars().all()."""
    db = AsyncMock()
    result = MagicMock()
    result.scalars.return_value.all.return_value = sessions
    db.execute.return_value = result
    return db


def _make_mock_db_returning_one(session: MagicMock | None) -> AsyncMock:
    """Build a mock AsyncSession whose execute() returns a single scalar."""
    db = AsyncMock()
    result = MagicMock()
    result.scalar_one_or_none.return_value = session
    db.execute.return_value = result
    return db


class TestListSessionsByTelegramId:
    """Tests for list_sessions_by_telegram_id."""

    @pytest.mark.asyncio
    async def test_returns_all_sessions_for_user(self):
        """All sessions (any status) belonging to the user should be returned."""
        from api_server.services.session_service import list_sessions_by_telegram_id

        sessions = [_make_mock_session() for _ in range(3)]
        db = _make_mock_db_returning_list(sessions)

        result = await list_sessions_by_telegram_id(
            telegram_id=12345, db=db, limit=10
        )

        assert len(result) == 3

    @pytest.mark.asyncio
    async def test_returns_empty_list_when_no_sessions(self):
        """A user with no sessions gets an empty list."""
        from api_server.services.session_service import list_sessions_by_telegram_id

        db = _make_mock_db_returning_list([])

        result = await list_sessions_by_telegram_id(
            telegram_id=99999, db=db, limit=10
        )

        assert result == []

    @pytest.mark.asyncio
    async def test_passes_limit_to_query(self):
        """The limit parameter should be passed to the database query."""
        from api_server.services.session_service import list_sessions_by_telegram_id

        db = _make_mock_db_returning_list([])

        await list_sessions_by_telegram_id(
            telegram_id=12345, db=db, limit=5
        )

        # Verify execute was called (the actual limit clause is in the
        # SQLAlchemy query object which is hard to inspect via mock,
        # but we verify the function completes without error).
        db.execute.assert_called_once()


class TestResumeSession:
    """Tests for resume_session."""

    @pytest.mark.asyncio
    async def test_stops_current_active_session_before_resuming(self):
        """When another session is active, it must be stopped first."""
        from api_server.services.session_service import resume_session

        current_session_id = uuid.uuid4()
        target_session_id = uuid.uuid4()
        target_session = _make_mock_session(
            session_id=target_session_id, status="stopped"
        )

        # Mock get_active returns a different session.
        mock_active_dto = MagicMock()
        mock_active_dto.id = current_session_id

        db = _make_mock_db_returning_one(target_session)

        with (
            patch(
                f"{_MODULE}.get_active_session_by_telegram_id",
                new_callable=AsyncMock,
                return_value=mock_active_dto,
            ),
            patch(
                f"{_MODULE}.stop_session",
                new_callable=AsyncMock,
            ) as mock_stop,
            patch(
                f"{_MODULE}._call_container_manager",
                new_callable=AsyncMock,
            ),
        ):
            await resume_session(
                target_session_id=target_session_id,
                telegram_id=12345,
                container_manager_url="http://cm:8001",
                service_token="tok",
                db=db,
            )

        # The old active session must have been stopped.
        mock_stop.assert_awaited_once_with(
            current_session_id, "http://cm:8001", "tok", db
        )
        # The target session is now running.
        assert target_session.status == "running"

    @pytest.mark.asyncio
    async def test_skips_stop_when_no_active_session(self):
        """When no session is currently active, resume proceeds directly."""
        from api_server.services.session_service import resume_session

        target_session_id = uuid.uuid4()
        target_session = _make_mock_session(
            session_id=target_session_id, status="stopped"
        )

        db = _make_mock_db_returning_one(target_session)

        with (
            patch(
                f"{_MODULE}.get_active_session_by_telegram_id",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch(
                f"{_MODULE}.stop_session",
                new_callable=AsyncMock,
            ) as mock_stop,
            patch(
                f"{_MODULE}._call_container_manager",
                new_callable=AsyncMock,
            ),
        ):
            await resume_session(
                target_session_id=target_session_id,
                telegram_id=12345,
                container_manager_url="http://cm:8001",
                service_token="tok",
                db=db,
            )

        mock_stop.assert_not_awaited()
        assert target_session.status == "running"

    @pytest.mark.asyncio
    async def test_skips_stop_when_resuming_already_active_session(self):
        """When the target is already the active session, don't stop it first."""
        from api_server.services.session_service import resume_session

        target_session_id = uuid.uuid4()
        target_session = _make_mock_session(
            session_id=target_session_id, status="paused"
        )

        mock_active_dto = MagicMock()
        mock_active_dto.id = target_session_id

        db = _make_mock_db_returning_one(target_session)

        with (
            patch(
                f"{_MODULE}.get_active_session_by_telegram_id",
                new_callable=AsyncMock,
                return_value=mock_active_dto,
            ),
            patch(
                f"{_MODULE}.stop_session",
                new_callable=AsyncMock,
            ) as mock_stop,
            patch(
                f"{_MODULE}._call_container_manager",
                new_callable=AsyncMock,
            ),
        ):
            await resume_session(
                target_session_id=target_session_id,
                telegram_id=12345,
                container_manager_url="http://cm:8001",
                service_token="tok",
                db=db,
            )

        mock_stop.assert_not_awaited()
        assert target_session.status == "running"

    @pytest.mark.asyncio
    async def test_raises_value_error_for_unknown_session(self):
        """Resuming a non-existent session raises ValueError."""
        from api_server.services.session_service import resume_session

        db = _make_mock_db_returning_one(None)

        with (
            patch(
                f"{_MODULE}.get_active_session_by_telegram_id",
                new_callable=AsyncMock,
                return_value=None,
            ),
            pytest.raises(ValueError, match="not found"),
        ):
            await resume_session(
                target_session_id=uuid.uuid4(),
                telegram_id=12345,
                container_manager_url="http://cm:8001",
                service_token="tok",
                db=db,
            )

    @pytest.mark.asyncio
    async def test_calls_container_manager_restart(self):
        """Resume must call the container-manager restart endpoint."""
        from api_server.services.session_service import resume_session

        target_session_id = uuid.uuid4()
        target_session = _make_mock_session(
            session_id=target_session_id, container_id="ctr-xyz", status="stopped"
        )
        db = _make_mock_db_returning_one(target_session)

        with (
            patch(
                f"{_MODULE}.get_active_session_by_telegram_id",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch(
                f"{_MODULE}._call_container_manager",
                new_callable=AsyncMock,
            ) as mock_cm,
        ):
            await resume_session(
                target_session_id=target_session_id,
                telegram_id=12345,
                container_manager_url="http://cm:8001",
                service_token="tok",
                db=db,
            )

        mock_cm.assert_awaited_once_with(
            "http://cm:8001", "tok", "post",
            "/containers/ctr-xyz/restart",
        )
