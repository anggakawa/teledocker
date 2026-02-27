"""Message-related data transfer objects and request schemas."""

from datetime import datetime
from enum import Enum
from uuid import UUID

from pydantic import BaseModel


class MessageDirection(str, Enum):
    inbound = "inbound"
    outbound = "outbound"


class ContentType(str, Enum):
    text = "text"
    file = "file"
    command = "command"
    system = "system"


class MessageDTO(BaseModel):
    """Full message audit record returned by the API."""

    id: UUID
    session_id: UUID
    direction: MessageDirection
    content_type: ContentType
    content: str
    telegram_msg_id: int | None
    processing_ms: int | None
    created_at: datetime

    model_config = {"from_attributes": True}


class ExecRequest(BaseModel):
    """Payload for POST /api/v1/sessions/:id/exec."""

    command: str


class SendMessageRequest(BaseModel):
    """Payload for POST /api/v1/sessions/:id/message."""

    text: str
    telegram_msg_id: int | None = None
