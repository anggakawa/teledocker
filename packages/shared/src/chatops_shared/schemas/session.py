"""Session-related data transfer objects."""

from datetime import datetime
from enum import Enum
from uuid import UUID

from pydantic import BaseModel, Field


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
    # The SQLAlchemy attribute is `metadata_` (with underscore) because
    # `metadata` collides with the inherited Base.metadata attribute.
    metadata: dict | None = Field(default=None, alias="metadata_")
    created_at: datetime

    model_config = {"from_attributes": True, "populate_by_name": True}
