"""SQLAlchemy 2.0 mapped classes for the ChatOps database schema.

Tables:
- users: Telegram users with approval state and encrypted API keys.
- sessions: Per-user Docker container sessions with lifecycle status.
- messages: Append-only audit log of all inbound and outbound messages.

All UUIDs are generated server-side by PostgreSQL's gen_random_uuid().
All ENUM types are defined in PostgreSQL as well to enforce at the DB level.
"""

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    Enum,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    Text,
    Uuid,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.types import DateTime, String


class Base(DeclarativeBase):
    pass


# Mirror the string values of the shared enums so SQLAlchemy uses the same labels.
_user_role_enum = Enum("admin", "user", "guest", name="user_role")
_session_status_enum = Enum(
    "creating", "running", "paused", "stopped", "error", name="session_status"
)
_message_direction_enum = Enum("inbound", "outbound", name="message_direction")
_content_type_enum = Enum("text", "file", "command", "system", name="content_type")


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    telegram_id: Mapped[int] = mapped_column(BigInteger, nullable=False, unique=True)
    telegram_username: Mapped[str | None] = mapped_column(String(255), nullable=True)
    display_name: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[str] = mapped_column(_user_role_enum, nullable=False, default="guest")
    is_approved: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    api_key_encrypted: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    api_key_iv: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    provider_config: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    max_containers: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    sessions: Mapped[list["Session"]] = relationship("Session", back_populates="user")


class Session(Base):
    __tablename__ = "sessions"
    __table_args__ = (
        Index("ix_sessions_user_id_status", "user_id", "status"),
        Index("ix_sessions_last_activity_at", "last_activity_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id"), nullable=False
    )
    container_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    container_name: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(
        _session_status_enum, nullable=False, default="creating"
    )
    agent_type: Mapped[str] = mapped_column(
        String(50), nullable=False, default="claude-code"
    )
    system_prompt: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_activity_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    metadata_: Mapped[dict | None] = mapped_column(
        "metadata", JSONB, nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    user: Mapped["User"] = relationship("User", back_populates="sessions")
    messages: Mapped[list["Message"]] = relationship(
        "Message", back_populates="session"
    )


class Message(Base):
    __tablename__ = "messages"
    __table_args__ = (
        Index("ix_messages_session_id_created_at", "session_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    session_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("sessions.id"), nullable=False
    )
    direction: Mapped[str] = mapped_column(_message_direction_enum, nullable=False)
    content_type: Mapped[str] = mapped_column(_content_type_enum, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    telegram_msg_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    processing_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    session: Mapped["Session"] = relationship("Session", back_populates="messages")
