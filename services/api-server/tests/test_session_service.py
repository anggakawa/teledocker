"""Tests for destroy_session 404 tolerance.

When destroy_session calls container-manager to delete a container, the
container may already be gone. The container-manager (or its reverse proxy)
returns HTTP 404. destroy_session must catch this and still clean up the
database record, rather than propagating the error and leaving the session
stuck forever.

These tests use lightweight mocks instead of real DB/HTTP connections,
following the same pattern as test_user_service.py.
"""

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

# ---------------------------------------------------------------------------
# Mirror of destroy_session's core contract
# ---------------------------------------------------------------------------

# We test the actual function via patch, but need a local reference
# to the module path for mocking _call_container_manager.
_MODULE = "api_server.services.session_service"


def _make_mock_session(
    session_id: uuid.UUID | None = None,
    container_id: str | None = "ctr-abc123",
) -> MagicMock:
    """Build a mock Session ORM object."""
    session = MagicMock()
    session.id = session_id or uuid.uuid4()
    session.container_id = container_id
    return session


def _make_mock_db(session: MagicMock | None = None) -> AsyncMock:
    """Build a mock AsyncSession that returns the given session from execute().

    If session is None, scalar_one_or_none returns None (session not found).
    """
    db = AsyncMock()
    result = MagicMock()
    result.scalar_one_or_none.return_value = session
    db.execute.return_value = result
    return db


def _make_http_status_error(status_code: int) -> httpx.HTTPStatusError:
    """Build an httpx.HTTPStatusError with the given status code."""
    response = httpx.Response(status_code=status_code, request=httpx.Request("DELETE", "http://test"))
    return httpx.HTTPStatusError(
        message=f"HTTP {status_code}",
        request=response.request,
        response=response,
    )


# ---------------------------------------------------------------------------
# Tests: destroy_session 404 handling
# ---------------------------------------------------------------------------


class TestDestroySessionsByStatus:
    """Verify bulk destroy by status iterates sessions and collects results."""

    @pytest.mark.asyncio
    async def test_destroys_all_matching_sessions(self):
        """All sessions with the target status should be destroyed."""
        from api_server.services.session_service import destroy_sessions_by_status

        sessions = [_make_mock_session() for _ in range(3)]
        db = AsyncMock()
        result_mock = MagicMock()
        result_mock.scalars.return_value.all.return_value = sessions
        db.execute.return_value = result_mock

        with patch(
            f"{_MODULE}.destroy_session",
            new_callable=AsyncMock,
        ) as mock_destroy:
            result = await destroy_sessions_by_status(
                status_filter="error",
                container_manager_url="http://container-manager:8001",
                service_token="test-token",
                db=db,
            )

        assert result == {"destroyed": 3, "failed": 0}
        assert mock_destroy.call_count == 3

    @pytest.mark.asyncio
    async def test_returns_zero_when_no_sessions_match(self):
        """Empty result set should return zeroed counts."""
        from api_server.services.session_service import destroy_sessions_by_status

        db = AsyncMock()
        result_mock = MagicMock()
        result_mock.scalars.return_value.all.return_value = []
        db.execute.return_value = result_mock

        result = await destroy_sessions_by_status(
            status_filter="error",
            container_manager_url="http://container-manager:8001",
            service_token="test-token",
            db=db,
        )

        assert result == {"destroyed": 0, "failed": 0}

    @pytest.mark.asyncio
    async def test_counts_failures_separately(self):
        """Individual destroy failures should be counted, not stop the loop."""
        from api_server.services.session_service import destroy_sessions_by_status

        sessions = [_make_mock_session() for _ in range(3)]
        db = AsyncMock()
        result_mock = MagicMock()
        result_mock.scalars.return_value.all.return_value = sessions
        db.execute.return_value = result_mock

        # First and third succeed, second fails.
        side_effects = [None, RuntimeError("container stuck"), None]
        with patch(
            f"{_MODULE}.destroy_session",
            new_callable=AsyncMock,
            side_effect=side_effects,
        ):
            result = await destroy_sessions_by_status(
                status_filter="error",
                container_manager_url="http://container-manager:8001",
                service_token="test-token",
                db=db,
            )

        assert result == {"destroyed": 2, "failed": 1}

    @pytest.mark.asyncio
    async def test_rollback_called_on_failure_so_loop_continues(self):
        """After a destroy failure, db.rollback() must be called to reset
        the transaction so subsequent iterations don't hit PendingRollbackError.
        """
        from api_server.services.session_service import destroy_sessions_by_status

        sessions = [_make_mock_session() for _ in range(3)]
        db = AsyncMock()
        result_mock = MagicMock()
        result_mock.scalars.return_value.all.return_value = sessions
        db.execute.return_value = result_mock

        # Second call fails; first and third succeed.
        side_effects = [None, RuntimeError("IntegrityError simulation"), None]
        with patch(
            f"{_MODULE}.destroy_session",
            new_callable=AsyncMock,
            side_effect=side_effects,
        ):
            result = await destroy_sessions_by_status(
                status_filter="error",
                container_manager_url="http://container-manager:8001",
                service_token="test-token",
                db=db,
            )

        assert result == {"destroyed": 2, "failed": 1}
        # Rollback must have been called exactly once (on the single failure).
        db.rollback.assert_called_once()

    @pytest.mark.asyncio
    async def test_value_error_counted_as_destroyed_not_failed(self):
        """Sessions already gone (ValueError) should count as destroyed, not failed."""
        from api_server.services.session_service import destroy_sessions_by_status

        sessions = [_make_mock_session() for _ in range(3)]
        db = AsyncMock()
        result_mock = MagicMock()
        result_mock.scalars.return_value.all.return_value = sessions
        db.execute.return_value = result_mock

        # First succeeds, second and third are already gone.
        side_effects = [None, ValueError("Session ... not found"), ValueError("gone")]
        with patch(
            f"{_MODULE}.destroy_session",
            new_callable=AsyncMock,
            side_effect=side_effects,
        ):
            result = await destroy_sessions_by_status(
                status_filter="error",
                container_manager_url="http://container-manager:8001",
                service_token="test-token",
                db=db,
            )

        assert result == {"destroyed": 3, "failed": 0}
        # No rollback needed for ValueError — no dirty DB state.
        db.rollback.assert_not_called()

    @pytest.mark.asyncio
    async def test_passes_correct_args_to_destroy_session(self):
        """Each destroy_session call must receive the right session_id and config."""
        from api_server.services.session_service import destroy_sessions_by_status

        session = _make_mock_session()
        db = AsyncMock()
        result_mock = MagicMock()
        result_mock.scalars.return_value.all.return_value = [session]
        db.execute.return_value = result_mock

        with patch(
            f"{_MODULE}.destroy_session",
            new_callable=AsyncMock,
        ) as mock_destroy:
            await destroy_sessions_by_status(
                status_filter="paused",
                container_manager_url="http://cm:8001",
                service_token="tok",
                db=db,
            )

        mock_destroy.assert_called_once_with(
            session_id=session.id,
            container_manager_url="http://cm:8001",
            service_token="tok",
            db=db,
        )


class TestDestroySessionNotFoundHandling:
    """Verify destroy_session cleans up DB even when container is gone or unreachable."""

    @pytest.mark.asyncio
    async def test_deletes_session_when_container_manager_unreachable(self):
        """If container-manager is unreachable (DNS/network), session is still deleted."""
        from api_server.services.session_service import destroy_session

        session = _make_mock_session()
        db = _make_mock_db(session)

        with patch(
            f"{_MODULE}._call_container_manager",
            new_callable=AsyncMock,
            side_effect=httpx.ConnectError("[Errno -3] Temporary failure in name resolution"),
        ):
            await destroy_session(
                session_id=session.id,
                container_manager_url="http://container-manager:8001",
                service_token="test-token",
                db=db,
            )

        db.delete.assert_called_once_with(session)
        db.commit.assert_called()

    @pytest.mark.asyncio
    async def test_deletes_session_when_container_manager_returns_404(self):
        """If container-manager returns 404, the session record is still deleted."""
        from api_server.services.session_service import destroy_session

        session = _make_mock_session()
        db = _make_mock_db(session)

        with patch(
            f"{_MODULE}._call_container_manager",
            new_callable=AsyncMock,
            side_effect=_make_http_status_error(404),
        ):
            await destroy_session(
                session_id=session.id,
                container_manager_url="http://container-manager:8001",
                service_token="test-token",
                db=db,
            )

        db.delete.assert_called_once_with(session)
        db.commit.assert_called()

    @pytest.mark.asyncio
    async def test_propagates_non_404_http_errors(self):
        """HTTP errors other than 404 (e.g. 500) must still propagate."""
        from api_server.services.session_service import destroy_session

        session = _make_mock_session()
        db = _make_mock_db(session)

        with patch(
            f"{_MODULE}._call_container_manager",
            new_callable=AsyncMock,
            side_effect=_make_http_status_error(500),
        ), pytest.raises(httpx.HTTPStatusError):
            await destroy_session(
                session_id=session.id,
                container_manager_url="http://container-manager:8001",
                service_token="test-token",
                db=db,
            )

        # DB record must NOT be deleted when a real error occurs.
        db.delete.assert_not_called()

    @pytest.mark.asyncio
    async def test_deletes_session_normally_on_success(self):
        """When container-manager returns 204, session is deleted normally."""
        from api_server.services.session_service import destroy_session

        session = _make_mock_session()
        db = _make_mock_db(session)

        with patch(
            f"{_MODULE}._call_container_manager",
            new_callable=AsyncMock,
        ):
            await destroy_session(
                session_id=session.id,
                container_manager_url="http://container-manager:8001",
                service_token="test-token",
                db=db,
            )

        db.delete.assert_called_once_with(session)
        db.commit.assert_called()

    @pytest.mark.asyncio
    async def test_skips_container_manager_when_no_container_id(self):
        """Sessions without a container_id should skip the HTTP call entirely."""
        from api_server.services.session_service import destroy_session

        session = _make_mock_session(container_id=None)
        db = _make_mock_db(session)

        with patch(
            f"{_MODULE}._call_container_manager",
            new_callable=AsyncMock,
        ) as mock_call:
            await destroy_session(
                session_id=session.id,
                container_manager_url="http://container-manager:8001",
                service_token="test-token",
                db=db,
            )

        mock_call.assert_not_called()
        db.delete.assert_called_once_with(session)

    @pytest.mark.asyncio
    async def test_raises_value_error_for_unknown_session(self):
        """Attempting to destroy a non-existent session should raise ValueError."""
        from api_server.services.session_service import destroy_session

        db = _make_mock_db(session=None)

        with pytest.raises(ValueError, match="not found"):
            await destroy_session(
                session_id=uuid.uuid4(),
                container_manager_url="http://container-manager:8001",
                service_token="test-token",
                db=db,
            )

    @pytest.mark.asyncio
    async def test_logs_warning_on_404(self):
        """A warning should be logged when container-manager returns 404."""
        from api_server.services.session_service import destroy_session

        session = _make_mock_session()
        db = _make_mock_db(session)

        with patch(
            f"{_MODULE}._call_container_manager",
            new_callable=AsyncMock,
            side_effect=_make_http_status_error(404),
        ), patch(f"{_MODULE}.logger") as mock_logger:
            await destroy_session(
                session_id=session.id,
                container_manager_url="http://container-manager:8001",
                service_token="test-token",
                db=db,
            )

            mock_logger.warning.assert_called_once()
            assert "not found" in mock_logger.warning.call_args[0][0]
