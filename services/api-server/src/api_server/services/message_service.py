"""Append-only message audit log service."""

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api_server.db.models import Message
from chatops_shared.schemas.message import MessageDTO


async def log_message(
    session_id: uuid.UUID,
    direction: str,
    content_type: str,
    content: str,
    db: AsyncSession,
    telegram_msg_id: int | None = None,
    processing_ms: int | None = None,
) -> MessageDTO:
    """Append a message record to the audit log."""
    message = Message(
        session_id=session_id,
        direction=direction,
        content_type=content_type,
        content=content,
        telegram_msg_id=telegram_msg_id,
        processing_ms=processing_ms,
    )
    db.add(message)
    await db.commit()
    await db.refresh(message)
    return MessageDTO.model_validate(message)


async def get_message_history(
    session_id: uuid.UUID,
    limit: int,
    db: AsyncSession,
) -> list[MessageDTO]:
    """Return the most recent N messages for a session, ordered oldest first."""
    result = await db.execute(
        select(Message)
        .where(Message.session_id == session_id)
        .order_by(Message.created_at.desc())
        .limit(limit)
    )
    messages = result.scalars().all()
    # Reverse so oldest is first in the returned list.
    return [MessageDTO.model_validate(m) for m in reversed(messages)]
