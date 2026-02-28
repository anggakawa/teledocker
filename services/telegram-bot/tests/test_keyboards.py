"""Tests for keyboard builders, focusing on bulk destroy buttons."""

from telegram_bot.keyboards import (
    BULK_DESTROYABLE_STATUSES,
    admin_sessions_keyboard,
)


class TestAdminSessionsKeyboard:
    """Verify admin_sessions_keyboard generates correct button layouts."""

    def test_individual_destroy_buttons(self):
        """Each session gets its own destroy button."""
        sessions = [(1, "aaa-111"), (2, "bbb-222")]
        keyboard = admin_sessions_keyboard(sessions)
        buttons = keyboard.inline_keyboard

        assert len(buttons) == 2
        assert buttons[0][0].text == "Destroy #1"
        assert buttons[0][0].callback_data == "admin_destroy:aaa-111"
        assert buttons[1][0].text == "Destroy #2"
        assert buttons[1][0].callback_data == "admin_destroy:bbb-222"

    def test_bulk_buttons_appear_for_destroyable_statuses(self):
        """Bulk destroy buttons appear for error, paused, stopped."""
        sessions = [(1, "aaa-111")]
        status_counts = {"error": 3, "paused": 2, "stopped": 1}
        keyboard = admin_sessions_keyboard(sessions, status_counts)
        buttons = keyboard.inline_keyboard

        # 1 individual + 3 bulk buttons
        assert len(buttons) == 4

        bulk_buttons = buttons[1:]
        bulk_texts = [row[0].text for row in bulk_buttons]
        assert "Destroy all error (3)" in bulk_texts
        assert "Destroy all paused (2)" in bulk_texts
        assert "Destroy all stopped (1)" in bulk_texts

    def test_bulk_buttons_use_correct_callback_data(self):
        """Bulk button callback data follows admin_destroy_status:<status> format."""
        sessions = [(1, "aaa")]
        status_counts = {"error": 1}
        keyboard = admin_sessions_keyboard(sessions, status_counts)
        bulk_button = keyboard.inline_keyboard[1][0]

        assert bulk_button.callback_data == "admin_destroy_status:error"

    def test_no_bulk_button_for_running(self):
        """Running sessions must never get a bulk destroy button."""
        sessions = [(1, "aaa")]
        status_counts = {"running": 5, "error": 1}
        keyboard = admin_sessions_keyboard(sessions, status_counts)
        buttons = keyboard.inline_keyboard

        # 1 individual + 1 bulk (error only, not running)
        assert len(buttons) == 2
        assert "running" not in buttons[1][0].callback_data

    def test_no_bulk_button_for_creating(self):
        """Creating sessions must never get a bulk destroy button."""
        sessions = [(1, "aaa")]
        status_counts = {"creating": 2}
        keyboard = admin_sessions_keyboard(sessions, status_counts)
        buttons = keyboard.inline_keyboard

        # Only the individual button, no bulk buttons
        assert len(buttons) == 1

    def test_no_bulk_buttons_when_counts_are_zero(self):
        """Statuses with zero count should not produce buttons."""
        sessions = [(1, "aaa")]
        status_counts = {"error": 0, "paused": 0}
        keyboard = admin_sessions_keyboard(sessions, status_counts)
        buttons = keyboard.inline_keyboard

        assert len(buttons) == 1

    def test_no_bulk_buttons_when_status_counts_is_none(self):
        """When status_counts is not provided, no bulk buttons appear."""
        sessions = [(1, "aaa"), (2, "bbb")]
        keyboard = admin_sessions_keyboard(sessions)
        buttons = keyboard.inline_keyboard

        assert len(buttons) == 2

    def test_bulk_buttons_sorted_alphabetically(self):
        """Bulk buttons appear in alphabetical order by status name."""
        sessions = [(1, "aaa")]
        status_counts = {"stopped": 1, "error": 2, "paused": 3}
        keyboard = admin_sessions_keyboard(sessions, status_counts)
        bulk_buttons = keyboard.inline_keyboard[1:]
        statuses = [row[0].callback_data.split(":")[1] for row in bulk_buttons]

        assert statuses == ["error", "paused", "stopped"]

    def test_bulk_destroyable_statuses_constant(self):
        """The constant should contain exactly error, paused, stopped."""
        assert BULK_DESTROYABLE_STATUSES == {"error", "paused", "stopped"}
