"""Business logic for Docker session lifecycle management."""

import logging
import uuid

import httpx
from sqlalchemy import select, update
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
    # Mark any stale "creating" sessions for this user as "error".
    # This prevents MultipleResultsFound in get_active_session queries
    # when a previous creation crashed before transitioning out of "creating".
    await db.execute(
        update(Session)
        .where(
            Session.user_id == user_id,
            Session.status == "creating",
        )
        .values(status="error")
    )

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
    """Return the most recent running/paused/creating session for this user.

    Multiple active sessions can exist if a previous creation attempt
    crashed before transitioning to 'error'. We pick the newest one and
    let cleanup handle the rest.
    """
    result = await db.execute(
        select(Session)
        .where(
            Session.user_id == user_id,
            Session.status.in_(["running", "paused", "creating"]),
        )
        .order_by(Session.created_at.desc())
        .limit(1)
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
        try:
            await _call_container_manager(
                container_manager_url, service_token, "delete",
                f"/containers/{session.container_id}",
            )
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                logger.warning(
                    "Container %s not found in container-manager, "
                    "proceeding with DB cleanup.",
                    session.container_id,
                )
            else:
                raise
        except httpx.ConnectError:
            logger.warning(
                "Cannot reach container-manager to delete container %s "
                "(DNS/network failure), proceeding with DB cleanup.",
                session.container_id,
            )

    await db.delete(session)
    await db.commit()
    logger.info("Destroyed session %s", session_id)


async def destroy_sessions_by_status(
    status_filter: str,
    container_manager_url: str,
    service_token: str,
    db: AsyncSession,
) -> dict[str, int]:
    """Destroy all sessions matching the given status.

    Loops through each matching session and calls destroy_session(),
    which handles both container cleanup and DB deletion. Continues
    on individual failures so one stuck container does not block the rest.

    Returns {"destroyed": N, "failed": N}.
    """
    result = await db.execute(
        select(Session).where(Session.status == status_filter)
    )
    # Extract IDs upfront: after db.commit()/rollback() inside the loop,
    # SQLAlchemy expires ORM objects. Accessing .id on an expired object
    # triggers a lazy load, which fails with MissingGreenlet under AsyncSession.
    session_ids = [s.id for s in result.scalars().all()]

    destroyed = 0
    failed = 0

    for session_id in session_ids:
        try:
            await destroy_session(
                session_id=session_id,
                container_manager_url=container_manager_url,
                service_token=service_token,
                db=db,
            )
            destroyed += 1
        except ValueError:
            # Session already gone (concurrent delete, cascade, or stale
            # identity map after a prior commit). The goal was to remove
            # it, and it is removed — count as success.
            logger.info(
                "Session %s already removed during bulk cleanup, skipping.",
                session_id,
            )
            destroyed += 1
        except Exception:
            logger.exception(
                "Failed to destroy session %s during bulk cleanup", session_id
            )
            await db.rollback()
            failed += 1

    return {"destroyed": destroyed, "failed": failed}


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
    """Find the most recent running/paused/creating session for a Telegram user.

    Joins sessions to users via user_id to look up by telegram_id.
    Multiple active sessions can exist after crashes or race conditions,
    so we pick the newest one to avoid MultipleResultsFound.
    """
    result = await db.execute(
        select(Session)
        .join(User, Session.user_id == User.id)
        .where(
            User.telegram_id == telegram_id,
            Session.status.in_(["running", "paused", "creating"]),
        )
        .order_by(Session.created_at.desc())
        .limit(1)
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
