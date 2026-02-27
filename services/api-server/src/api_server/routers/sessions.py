"""Session management and container interaction endpoints.

SSE (Server-Sent Events) is used for streaming exec output and AI responses
so Telegram bot can start forwarding chunks immediately without waiting for
the full response to complete. This is critical for long-running AI responses.
"""

import json
import logging
import time
import uuid

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile, status
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from chatops_shared.schemas.message import ExecRequest, SendMessageRequest
from chatops_shared.schemas.session import SessionDTO

from api_server.config import settings
from api_server.db.engine import get_db
from api_server.dependencies import get_redis, verify_service_token
from api_server.services import message_service, session_service

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/v1/sessions",
    tags=["sessions"],
    dependencies=[Depends(verify_service_token)],
)


class CreateSessionRequest(ExecRequest):
    pass


from pydantic import BaseModel


class NewSessionRequest(BaseModel):
    user_id: uuid.UUID
    telegram_id: int
    agent_type: str = "claude-code"
    system_prompt: str | None = None


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

    async def generate():
        try:
            async with httpx.AsyncClient(timeout=300.0) as client:
                async with client.stream(
                    "POST",
                    f"{settings.container_manager_url}/containers/{container_id}/exec",
                    json={"command": payload.command},
                    headers={"X-Service-Token": settings.service_token},
                ) as response:
                    async for line in response.aiter_lines():
                        if line:
                            yield f"data: {line}\n\n"
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
        try:
            async with httpx.AsyncClient(timeout=300.0) as client:
                async with client.stream(
                    "POST",
                    f"{settings.container_manager_url}/containers/{container_id}/message",
                    json={"text": payload.text},
                    headers={"X-Service-Token": settings.service_token},
                ) as response:
                    async for line in response.aiter_lines():
                        if line:
                            full_response_parts.append(line)
                            yield f"data: {line}\n\n"
        except Exception as exc:
            error_payload = json.dumps({"error": str(exc)})
            yield f"data: {error_payload}\n\n"
        finally:
            yield "data: [DONE]\n\n"

            # Log the full outbound response after streaming completes.
            elapsed_ms = int((time.monotonic() - start_time) * 1000)
            full_response = "".join(full_response_parts)
            await message_service.log_message(
                session_id=session_id,
                direction="outbound",
                content_type="text",
                content=full_response,
                db=db,
                processing_ms=elapsed_ms,
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
