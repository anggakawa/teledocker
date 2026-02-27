"""Business logic for Docker session lifecycle management."""

import logging
import uuid

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import func

from chatops_shared.schemas.session import SessionDTO

from api_server.db.models import Session, User

logger = logging.getLogger(__name__)


def _make_container_name(telegram_id: int) -> str:
    """Generate a short, deterministic-looking container name."""
    short_uuid = str(uuid.uuid4())[:8]
    return f"chatops-{telegram_id}-{short_uuid}"


async def create_session(
    user_id: uuid.UUID,
    telegram_id: int,
    agent_type: str,
    system_prompt: str | None,
    container_manager_url: str,
    service_token: str,
    db: AsyncSession,
) -> SessionDTO:
    """Provision a Docker container and create a session record.

    1. Builds the session record with status=creating.
    2. Calls container-manager to create the container.
    3. Updates the session with the returned container_id and status=running.
    """
    container_name = _make_container_name(telegram_id)

    session = Session(
        user_id=user_id,
        container_name=container_name,
        status="creating",
        agent_type=agent_type,
        system_prompt=system_prompt,
    )
    db.add(session)
    await db.commit()
    await db.refresh(session)

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                f"{container_manager_url}/containers",
                json={
                    "session_id": str(session.id),
                    "container_name": container_name,
                    "user_id": str(user_id),
                    "telegram_id": telegram_id,
                },
                headers={"X-Service-Token": service_token},
            )
            response.raise_for_status()
            data = response.json()

        session.container_id = data["container_id"]
        session.status = "running"
    except Exception as exc:
        session.status = "error"
        logger.exception("Failed to create container for session %s: %s", session.id, exc)

    await db.commit()
    await db.refresh(session)
    return SessionDTO.model_validate(session)


async def get_session(session_id: uuid.UUID, db: AsyncSession) -> SessionDTO | None:
    """Fetch a session by ID. Returns None if not found."""
    result = await db.execute(select(Session).where(Session.id == session_id))
    session = result.scalar_one_or_none()
    if session is None:
        return None
    return SessionDTO.model_validate(session)


async def get_active_session(user_id: uuid.UUID, db: AsyncSession) -> SessionDTO | None:
    """Return the first running or paused session for this user."""
    result = await db.execute(
        select(Session).where(
            Session.user_id == user_id,
            Session.status.in_(["running", "paused", "creating"]),
        )
    )
    session = result.scalar_one_or_none()
    if session is None:
        return None
    return SessionDTO.model_validate(session)


async def _call_container_manager(
    container_manager_url: str,
    service_token: str,
    method: str,
    path: str,
) -> None:
    """Helper for simple fire-and-forget container manager calls."""
    async with httpx.AsyncClient(timeout=30.0) as client:
        request = getattr(client, method)
        response = await request(
            f"{container_manager_url}{path}",
            headers={"X-Service-Token": service_token},
        )
        response.raise_for_status()


async def stop_session(
    session_id: uuid.UUID,
    container_manager_url: str,
    service_token: str,
    db: AsyncSession,
) -> None:
    """Stop the container. Preserves the workspace volume."""
    result = await db.execute(select(Session).where(Session.id == session_id))
    session = result.scalar_one_or_none()
    if session is None:
        raise ValueError(f"Session {session_id} not found")

    await _call_container_manager(
        container_manager_url, service_token, "post",
        f"/containers/{session.container_id}/stop",
    )
    session.status = "stopped"
    await db.commit()


async def restart_session(
    session_id: uuid.UUID,
    container_manager_url: str,
    service_token: str,
    db: AsyncSession,
) -> None:
    """Restart the container. Workspace remains intact."""
    result = await db.execute(select(Session).where(Session.id == session_id))
    session = result.scalar_one_or_none()
    if session is None:
        raise ValueError(f"Session {session_id} not found")

    await _call_container_manager(
        container_manager_url, service_token, "post",
        f"/containers/{session.container_id}/restart",
    )
    session.status = "running"
    await db.commit()


async def destroy_session(
    session_id: uuid.UUID,
    container_manager_url: str,
    service_token: str,
    db: AsyncSession,
) -> None:
    """Remove container and volume, then delete the session record."""
    result = await db.execute(select(Session).where(Session.id == session_id))
    session = result.scalar_one_or_none()
    if session is None:
        raise ValueError(f"Session {session_id} not found")

    if session.container_id:
        await _call_container_manager(
            container_manager_url, service_token, "delete",
            f"/containers/{session.container_id}",
        )

    await db.delete(session)
    await db.commit()
    logger.info("Destroyed session %s", session_id)


async def list_sessions(
    status_filter: str | None, db: AsyncSession
) -> list[SessionDTO]:
    """List sessions, optionally filtered by status (e.g. 'running')."""
    query = select(Session)
    if status_filter:
        query = query.where(Session.status == status_filter)

    result = await db.execute(query)
    sessions = result.scalars().all()
    return [SessionDTO.model_validate(s) for s in sessions]


async def get_active_session_by_telegram_id(
    telegram_id: int, db: AsyncSession
) -> SessionDTO | None:
    """Find the first running/paused/creating session for a Telegram user.

    Joins sessions to users via user_id to look up by telegram_id.
    """
    result = await db.execute(
        select(Session)
        .join(User, Session.user_id == User.id)
        .where(
            User.telegram_id == telegram_id,
            Session.status.in_(["running", "paused", "creating"]),
        )
    )
    session = result.scalar_one_or_none()
    if session is None:
        return None
    return SessionDTO.model_validate(session)


async def update_session_status(
    session_id: uuid.UUID, new_status: str, db: AsyncSession
) -> SessionDTO:
    """Update only the status field of a session."""
    result = await db.execute(select(Session).where(Session.id == session_id))
    session = result.scalar_one_or_none()
    if session is None:
        raise ValueError(f"Session {session_id} not found")

    session.status = new_status
    await db.commit()
    await db.refresh(session)
    return SessionDTO.model_validate(session)


async def touch_session_activity(
    session_id: uuid.UUID, db: AsyncSession
) -> None:
    """Bump last_activity_at to now. Called when user sends a message."""
    result = await db.execute(select(Session).where(Session.id == session_id))
    session = result.scalar_one_or_none()
    if session is None:
        return

    session.last_activity_at = func.now()
    await db.commit()
