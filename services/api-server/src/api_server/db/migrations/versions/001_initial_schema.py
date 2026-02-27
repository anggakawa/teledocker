"""001_initial_schema

Create the initial users, sessions, and messages tables with all indexes.

Revision ID: 001
Revises:
Create Date: 2026-02-27

"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ENUM types must be created explicitly in PostgreSQL before the tables.
    user_role = postgresql.ENUM("admin", "user", "guest", name="user_role")
    session_status = postgresql.ENUM(
        "creating", "running", "paused", "stopped", "error", name="session_status"
    )
    message_direction = postgresql.ENUM(
        "inbound", "outbound", name="message_direction"
    )
    content_type = postgresql.ENUM(
        "text", "file", "command", "system", name="content_type"
    )
    user_role.create(op.get_bind())
    session_status.create(op.get_bind())
    message_direction.create(op.get_bind())
    content_type.create(op.get_bind())

    op.create_table(
        "users",
        sa.Column(
            "id",
            sa.Uuid(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("telegram_id", sa.BigInteger, nullable=False, unique=True),
        sa.Column("telegram_username", sa.String(255), nullable=True),
        sa.Column("display_name", sa.String(255), nullable=False),
        sa.Column(
            "role",
            postgresql.ENUM("admin", "user", "guest", name="user_role", create_type=False),
            nullable=False,
            server_default="guest",
        ),
        sa.Column("is_approved", sa.Boolean, nullable=False, server_default="false"),
        sa.Column("api_key_encrypted", sa.LargeBinary, nullable=True),
        sa.Column("api_key_iv", sa.LargeBinary, nullable=True),
        sa.Column("provider_config", postgresql.JSONB, nullable=True),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default="true"),
        sa.Column("max_containers", sa.Integer, nullable=False, server_default="1"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )

    op.create_table(
        "sessions",
        sa.Column(
            "id",
            sa.Uuid(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "user_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("users.id"),
            nullable=False,
        ),
        sa.Column("container_id", sa.String(64), nullable=True),
        sa.Column("container_name", sa.String(255), nullable=False),
        sa.Column(
            "status",
            postgresql.ENUM(
                "creating", "running", "paused", "stopped", "error",
                name="session_status",
                create_type=False,
            ),
            nullable=False,
            server_default="creating",
        ),
        sa.Column("agent_type", sa.String(50), nullable=False, server_default="claude-code"),
        sa.Column("system_prompt", sa.Text, nullable=True),
        sa.Column(
            "last_activity_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("metadata", postgresql.JSONB, nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_index("ix_sessions_user_id_status", "sessions", ["user_id", "status"])
    op.create_index("ix_sessions_last_activity_at", "sessions", ["last_activity_at"])

    op.create_table(
        "messages",
        sa.Column(
            "id",
            sa.Uuid(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "session_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("sessions.id"),
            nullable=False,
        ),
        sa.Column(
            "direction",
            postgresql.ENUM("inbound", "outbound", name="message_direction", create_type=False),
            nullable=False,
        ),
        sa.Column(
            "content_type",
            postgresql.ENUM(
                "text", "file", "command", "system", name="content_type", create_type=False
            ),
            nullable=False,
        ),
        sa.Column("content", sa.Text, nullable=False),
        sa.Column("telegram_msg_id", sa.BigInteger, nullable=True),
        sa.Column("processing_ms", sa.Integer, nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_index(
        "ix_messages_session_id_created_at", "messages", ["session_id", "created_at"]
    )


def downgrade() -> None:
    op.drop_table("messages")
    op.drop_table("sessions")
    op.drop_table("users")

    op.execute("DROP TYPE IF EXISTS content_type")
    op.execute("DROP TYPE IF EXISTS message_direction")
    op.execute("DROP TYPE IF EXISTS session_status")
    op.execute("DROP TYPE IF EXISTS user_role")
