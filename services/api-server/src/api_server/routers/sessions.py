"""Session management and container interaction endpoints.

SSE (Server-Sent Events) is used for streaming exec output and AI responses
so Telegram bot can start forwarding chunks immediately without waiting for
the full response to complete. This is critical for long-running AI responses.
"""

import asyncio
import json
import logging
import time
import uuid

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, UploadFile, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from chatops_shared.schemas.message import ExecRequest, SendMessageRequest
from chatops_shared.schemas.session import SessionDTO

from api_server.config import ApiServerSettings, settings
from api_server.db.engine import get_db, get_db_session
from api_server.db.models import User
from api_server.dependencies import verify_service_token
from api_server.services import message_service, session_service
from api_server.services.user_service import get_decrypted_api_key, get_user_model_by_id

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/v1/sessions",
    tags=["sessions"],
    dependencies=[Depends(verify_service_token)],
)


class CreateSessionRequest(ExecRequest):
    pass


class NewSessionRequest(BaseModel):
    user_id: uuid.UUID
    telegram_id: int
    agent_type: str = "claude-code"
    system_prompt: str | None = None


class UpdateStatusRequest(BaseModel):
    status: str


def _build_env_vars(user: User, app_settings: ApiServerSettings) -> dict[str, str]:
    """Build Claude CLI env vars from the user's API key and provider config.

    Returns an empty dict if no API key is stored — the agent will run
    without credentials (and Claude CLI will complain about login).
    """
    api_key = get_decrypted_api_key(user, app_settings)
    if not api_key:
        return {}

    provider_config = user.provider_config or {}
    provider = provider_config.get("provider", "anthropic")
    base_url = provider_config.get("base_url")

    if provider == "anthropic":
        return {"ANTHROPIC_API_KEY": api_key}

    # OpenRouter or custom provider — use base URL + auth token.
    env: dict[str, str] = {
        "ANTHROPIC_AUTH_TOKEN": api_key,
        "ANTHROPIC_API_KEY": "",  # Must be explicitly empty
    }
    if base_url:
        env["ANTHROPIC_BASE_URL"] = base_url
    return env


async def _log_outbound_message(
    session_id: uuid.UUID,
    full_response: str,
    elapsed_ms: int,
) -> None:
    """Log an outbound AI response in a fire-and-forget task.

    Runs in its own DB session so it is not subject to the SSE generator's
    cancellation scope. Errors are logged but never propagated — the user
    already received the streamed response, so a logging failure should not
    affect them.
    """
    try:
        async with get_db_session() as db:
            await message_service.log_message(
                session_id=session_id,
                direction="outbound",
                content_type="text",
                content=full_response,
                db=db,
                processing_ms=elapsed_ms,
            )
    except Exception:
        logger.exception(
            "Failed to log outbound message for session %s", session_id
        )


@router.get("", response_model=list[SessionDTO])
async def list_sessions(
    status_filter: str | None = Query(default=None, alias="status"),
    db: AsyncSession = Depends(get_db),
) -> list[SessionDTO]:
    """List sessions, optionally filtered by status."""
    return await session_service.list_sessions(status_filter, db)


@router.get("/active", response_model=SessionDTO)
async def get_active_session_by_telegram_id(
    telegram_id: int = Query(...),
    db: AsyncSession = Depends(get_db),
) -> SessionDTO:
    """Find the active (running/paused/creating) session for a Telegram user."""
    session = await session_service.get_active_session_by_telegram_id(telegram_id, db)
    if session is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No active session for this user",
        )
    return session


@router.post("", response_model=SessionDTO, status_code=status.HTTP_201_CREATED)
async def create_session(
    payload: NewSessionRequest,
    db: AsyncSession = Depends(get_db),
) -> SessionDTO:
    return await session_service.create_session(
        user_id=payload.user_id,
        telegram_id=payload.telegram_id,
        agent_type=payload.agent_type,
        system_prompt=payload.system_prompt,
        container_manager_url=settings.container_manager_url,
        service_token=settings.service_token,
        db=db,
    )


@router.get("/{session_id}", response_model=SessionDTO)
async def get_session(
    session_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
) -> SessionDTO:
    session = await session_service.get_session(session_id, db)
    if session is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session not found")
    return session


@router.post("/{session_id}/stop", status_code=status.HTTP_204_NO_CONTENT)
async def stop_session(
    session_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
) -> None:
    try:
        await session_service.stop_session(
            session_id, settings.container_manager_url, settings.service_token, db
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))


@router.post("/{session_id}/restart", status_code=status.HTTP_204_NO_CONTENT)
async def restart_session(
    session_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
) -> None:
    try:
        await session_service.restart_session(
            session_id, settings.container_manager_url, settings.service_token, db
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))


@router.delete("/{session_id}", status_code=status.HTTP_204_NO_CONTENT)
async def destroy_session(
    session_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
) -> None:
    try:
        await session_service.destroy_session(
            session_id, settings.container_manager_url, settings.service_token, db
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))


@router.patch("/{session_id}/status", response_model=SessionDTO)
async def update_session_status(
    session_id: uuid.UUID,
    payload: UpdateStatusRequest,
    db: AsyncSession = Depends(get_db),
) -> SessionDTO:
    """Update just the status field of a session (used by cleanup/health)."""
    try:
        return await session_service.update_session_status(
            session_id, payload.status, db
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))


@router.post("/{session_id}/exec")
async def exec_command(
    session_id: uuid.UUID,
    payload: ExecRequest,
    db: AsyncSession = Depends(get_db),
) -> StreamingResponse:
    """Execute a shell command inside the container. Streams output via SSE."""
    session = await session_service.get_session(session_id, db)
    if session is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session not found")

    container_id = session.container_id

    # Fetch env vars so exec'd commands that invoke Claude CLI have credentials.
    user = await get_user_model_by_id(session.user_id, db)
    env_vars = _build_env_vars(user, settings) if user else {}

    async def generate():
        try:
            async with httpx.AsyncClient(timeout=300.0) as client:
                async with client.stream(
                    "POST",
                    f"{settings.container_manager_url}/containers/{container_id}/exec",
                    json={"command": payload.command, "env_vars": env_vars},
                    headers={"X-Service-Token": settings.service_token},
                ) as response:
                    async for line in response.aiter_lines():
                        if not line or not line.startswith("data: "):
                            continue
                        payload_data = line[6:]
                        if payload_data == "[DONE]":
                            break
                        yield f"data: {payload_data}\n\n"
        except Exception as exc:
            error_payload = json.dumps({"error": str(exc)})
            yield f"data: {error_payload}\n\n"
        finally:
            yield "data: [DONE]\n\n"

    return StreamingResponse(generate(), media_type="text/event-stream")


@router.post("/{session_id}/message")
async def send_message(
    session_id: uuid.UUID,
    payload: SendMessageRequest,
    db: AsyncSession = Depends(get_db),
) -> StreamingResponse:
    """Send a message to the AI agent. Streams the response via SSE."""
    session = await session_service.get_session(session_id, db)
    if session is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session not found")

    start_time = time.monotonic()
    container_id = session.container_id

    # Fetch the user model to build per-message env vars (API key + provider).
    user = await get_user_model_by_id(session.user_id, db)
    env_vars = _build_env_vars(user, settings) if user else {}

    has_env_vars = bool(env_vars)
    logger.info(
        "send_message session=%s container=%s has_env_vars=%s",
        session_id, container_id, has_env_vars,
    )

    # Bump last_activity_at so idle cleanup knows this session is alive.
    await session_service.touch_session_activity(session_id, db)

    # Log the inbound message immediately.
    await message_service.log_message(
        session_id=session_id,
        direction="inbound",
        content_type="text",
        content=payload.text,
        db=db,
        telegram_msg_id=payload.telegram_msg_id,
    )

    full_response_parts: list[str] = []

    async def generate():
        event_count = 0
        try:
            async with httpx.AsyncClient(timeout=300.0) as client:
                async with client.stream(
                    "POST",
                    f"{settings.container_manager_url}/containers/{container_id}/message",
                    json={"text": payload.text, "env_vars": env_vars},
                    headers={"X-Service-Token": settings.service_token},
                ) as response:
                    async for line in response.aiter_lines():
                        if not line or not line.startswith("data: "):
                            continue
                        payload_data = line[6:]
                        if payload_data == "[DONE]":
                            break
                        yield f"data: {payload_data}\n\n"
                        event_count += 1

                        # Extract readable text for database logging.
                        try:
                            parsed = json.loads(payload_data)

                            # New structured event path.
                            event = parsed.get("event")
                            if event and event.get("type") == "text_delta":
                                text = event.get("text", "")
                                if text:
                                    full_response_parts.append(text)

                            # Legacy chunk path (backward compat).
                            chunk = parsed.get("chunk", "")
                            if chunk:
                                full_response_parts.append(chunk)
                        except json.JSONDecodeError:
                            pass
        except Exception as exc:
            error_payload = json.dumps({"error": str(exc)})
            yield f"data: {error_payload}\n\n"
        finally:
            yield "data: [DONE]\n\n"

            # Log outbound response in a fire-and-forget task. This runs
            # outside the SSE generator's cancel scope, so CancelledError
            # from client disconnect won't corrupt the DB session.
            elapsed_ms = int((time.monotonic() - start_time) * 1000)
            full_response = "".join(full_response_parts)
            logger.info(
                "send_message done session=%s events=%d response_len=%d elapsed_ms=%d",
                session_id, event_count, len(full_response), elapsed_ms,
            )
            asyncio.create_task(
                _log_outbound_message(session_id, full_response, elapsed_ms)
            )

    return StreamingResponse(generate(), media_type="text/event-stream")


@router.post("/{session_id}/upload", status_code=status.HTTP_200_OK)
async def upload_file(
    session_id: uuid.UUID,
    file: UploadFile,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Upload a file to the container's /workspace directory."""
    session = await session_service.get_session(session_id, db)
    if session is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session not found")

    file_content = await file.read()
    container_id = session.container_id

    async with httpx.AsyncClient(timeout=60.0) as client:
        response = await client.post(
            f"{settings.container_manager_url}/containers/{container_id}/upload",
            content=file_content,
            headers={
                "X-Service-Token": settings.service_token,
                "X-Filename": file.filename or "upload",
            },
        )
        response.raise_for_status()

    return {"filename": file.filename, "size": len(file_content)}


@router.get("/{session_id}/download/{file_path:path}")
async def download_file(
    session_id: uuid.UUID,
    file_path: str,
    db: AsyncSession = Depends(get_db),
) -> StreamingResponse:
    """Stream a file from the container's /workspace directory."""
    session = await session_service.get_session(session_id, db)
    if session is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session not found")

    container_id = session.container_id

    async def stream_file():
        async with httpx.AsyncClient(timeout=60.0) as client:
            async with client.stream(
                "GET",
                f"{settings.container_manager_url}/containers/{container_id}/download/{file_path}",
                headers={"X-Service-Token": settings.service_token},
            ) as response:
                response.raise_for_status()
                async for chunk in response.aiter_bytes():
                    yield chunk

    return StreamingResponse(
        stream_file(),
        media_type="application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{file_path.split("/")[-1]}"'},
    )
