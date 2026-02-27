"""Session-related data transfer objects."""

from datetime import datetime
from enum import Enum
from uuid import UUID

from pydantic import BaseModel


class SessionStatus(str, Enum):
    creating = "creating"
    running = "running"
    paused = "paused"
    stopped = "stopped"
    error = "error"


class SessionDTO(BaseModel):
    """Full session record returned by the API."""

    id: UUID
    user_id: UUID
    container_id: str | None
    container_name: str
    status: SessionStatus
    agent_type: str
    system_prompt: str | None
    last_activity_at: datetime
    metadata: dict | None
    created_at: datetime

    model_config = {"from_attributes": True}
