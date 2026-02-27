"""002_cascade_delete_messages

Replace the messages.session_id FK constraint with ON DELETE CASCADE
so that deleting a session automatically removes its messages at the
database level, preventing IntegrityError on NOT NULL violation.

Revision ID: 002
Revises: 001
Create Date: 2026-02-28

"""

from alembic import op

revision = "002"
down_revision = "001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # PostgreSQL cannot alter a FK constraint in-place;
    # we must drop and recreate it with the new ON DELETE rule.
    op.drop_constraint("messages_session_id_fkey", "messages", type_="foreignkey")
    op.create_foreign_key(
        "messages_session_id_fkey",
        "messages",
        "sessions",
        ["session_id"],
        ["id"],
        ondelete="CASCADE",
    )


def downgrade() -> None:
    op.drop_constraint("messages_session_id_fkey", "messages", type_="foreignkey")
    op.create_foreign_key(
        "messages_session_id_fkey",
        "messages",
        "sessions",
        ["session_id"],
        ["id"],
    )
