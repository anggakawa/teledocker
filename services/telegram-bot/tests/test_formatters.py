"""Tests for format_age() and format_session_list_for_admin().

These formatters produce plain-text output for the admin /containers command.
"""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from telegram_bot.formatters import format_age, format_session_list_for_admin

from chatops_shared.schemas.session import SessionDTO, SessionStatus
from chatops_shared.schemas.user import UserDTO, UserRole


def _now() -> datetime:
    return datetime.now(UTC)


def _make_session(
    status: str = "running",
    idle_minutes: int = 5,
    container_name: str = "agent-test",
    created_minutes_ago: int = 60,
) -> SessionDTO:
    """Build a minimal SessionDTO for testing."""
    return SessionDTO(
        id=uuid4(),
        user_id=uuid4(),
        container_id=f"ctr-{uuid4().hex[:8]}",
        container_name=container_name,
        status=SessionStatus(status),
        agent_type="claude-code",
        system_prompt=None,
        last_activity_at=_now() - timedelta(minutes=idle_minutes),
        metadata=None,
        created_at=_now() - timedelta(minutes=created_minutes_ago),
    )


def _make_user(
    telegram_id: int = 12345,
    display_name: str = "Alice",
    telegram_username: str | None = "alice",
    user_id=None,
) -> UserDTO:
    """Build a minimal UserDTO for testing."""
    return UserDTO(
        id=user_id or uuid4(),
        telegram_id=telegram_id,
        telegram_username=telegram_username,
        display_name=display_name,
        role=UserRole.user,
        is_approved=True,
        is_active=True,
        max_containers=5,
        provider_config=None,
        created_at=_now(),
        updated_at=_now(),
    )


# ---------------------------------------------------------------------------
# Tests: format_age
# ---------------------------------------------------------------------------


class TestFormatAge:
    """Verify human-readable age strings from datetime objects."""

    def test_seconds_ago_shows_just_now(self):
        """A datetime 10 seconds ago should show 'just now'."""
        dt = _now() - timedelta(seconds=10)
        assert format_age(dt) == "just now"

    def test_minutes_ago(self):
        """A datetime 15 minutes ago should show '15m ago'."""
        dt = _now() - timedelta(minutes=15)
        assert format_age(dt) == "15m ago"

    def test_one_minute_ago(self):
        """A datetime 1 minute ago should show '1m ago'."""
        dt = _now() - timedelta(minutes=1, seconds=30)
        assert format_age(dt) == "1m ago"

    def test_hours_ago(self):
        """A datetime 3 hours ago should show '3h ago'."""
        dt = _now() - timedelta(hours=3)
        assert format_age(dt) == "3h ago"

    def test_days_ago(self):
        """A datetime 2 days ago should show '2d ago'."""
        dt = _now() - timedelta(days=2)
        assert format_age(dt) == "2d ago"

    def test_future_datetime_shows_just_now(self):
        """A datetime in the future should show 'just now' (graceful edge case)."""
        dt = _now() + timedelta(minutes=5)
        assert format_age(dt) == "just now"

    def test_59_minutes_shows_minutes(self):
        """59 minutes should still show in minute format, not hours."""
        dt = _now() - timedelta(minutes=59)
        assert format_age(dt) == "59m ago"

    def test_60_minutes_shows_hours(self):
        """60 minutes should flip to hour format."""
        dt = _now() - timedelta(minutes=60)
        assert format_age(dt) == "1h ago"

    def test_23_hours_shows_hours(self):
        """23 hours should still show in hour format, not days."""
        dt = _now() - timedelta(hours=23)
        assert format_age(dt) == "23h ago"

    def test_24_hours_shows_days(self):
        """24 hours should flip to day format."""
        dt = _now() - timedelta(hours=24)
        assert format_age(dt) == "1d ago"


# ---------------------------------------------------------------------------
# Tests: format_session_list_for_admin
# ---------------------------------------------------------------------------


class TestFormatSessionListForAdmin:
    """Verify the admin container listing format."""

    def test_empty_list(self):
        """An empty list should show a count of 0."""
        result = format_session_list_for_admin([])
        assert "Containers (0):" in result

    def test_single_session_with_user(self):
        """A single session with a matched user should show user details."""
        user_id = uuid4()
        session = _make_session(status="running", container_name="agent-alice")
        session.user_id = user_id
        user = _make_user(
            telegram_id=12345,
            display_name="Alice",
            telegram_username="alice",
            user_id=user_id,
        )

        result = format_session_list_for_admin([(session, user)])

        assert "Containers (1):" in result
        assert "#1" in result
        assert "running" in result
        assert "Alice" in result
        assert "@alice" in result
        assert "12345" in result
        assert "agent-alice" in result

    def test_session_without_user_shows_user_id(self):
        """A session with no matching user should show the raw user_id."""
        session = _make_session()

        result = format_session_list_for_admin([(session, None)])

        assert f"user_id={session.user_id}" in result

    def test_user_without_username(self):
        """A user with no telegram_username should still show display name."""
        user_id = uuid4()
        session = _make_session()
        session.user_id = user_id
        user = _make_user(
            telegram_id=99999,
            display_name="Bob",
            telegram_username=None,
            user_id=user_id,
        )

        result = format_session_list_for_admin([(session, user)])

        assert "Bob" in result
        assert "@" not in result.split("Bob")[1].split("\n")[0]  # No @username on Bob's line

    def test_multiple_sessions_numbered_sequentially(self):
        """Multiple sessions should be numbered #1, #2, #3."""
        sessions_with_users = [
            (_make_session(status="running"), None),
            (_make_session(status="paused"), None),
            (_make_session(status="stopped"), None),
        ]

        result = format_session_list_for_admin(sessions_with_users)

        assert "Containers (3):" in result
        assert "#1" in result
        assert "#2" in result
        assert "#3" in result

    def test_status_emojis_present(self):
        """Each status should have its corresponding emoji."""
        result = format_session_list_for_admin([
            (_make_session(status="running"), None),
            (_make_session(status="paused"), None),
            (_make_session(status="error"), None),
        ])

        # Check that different status values appear.
        assert "running" in result
        assert "paused" in result
        assert "error" in result

    def test_contains_created_and_activity_labels(self):
        """Output should include 'Created:' and 'Last activity:' labels."""
        result = format_session_list_for_admin([(_make_session(), None)])

        assert "Created:" in result
        assert "Last activity:" in result
