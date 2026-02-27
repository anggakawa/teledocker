"""HTTP endpoints for container lifecycle and interaction operations."""

import asyncio
import json
import logging
import shlex
from pathlib import PurePosixPath

import websockets
from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from container_manager.config import settings
from container_manager.docker_client import DockerClient

_WORKSPACE_ROOT = PurePosixPath("/workspace")

logger = logging.getLogger(__name__)

router = APIRouter(tags=["containers"])

# The DockerClient instance is initialized at startup and stored in app.state.
# We access it through a simple dependency rather than importing a global.


def get_docker_client(request: Request) -> DockerClient:
    return request.app.state.docker_client


def verify_token(x_service_token: str = Header(..., alias="X-Service-Token")) -> None:
    if x_service_token != settings.service_token:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Invalid service token"
        )


def _validate_workspace_path(user_path: str) -> str:
    """Resolve a user-supplied path and ensure it stays within /workspace.

    Returns the safe absolute path string. Raises HTTPException on traversal.
    """
    # Resolve against /workspace to canonicalize any '..' sequences.
    resolved = PurePosixPath("/workspace") / user_path
    # Normalize the path (collapse .., ., etc.).
    normalized = PurePosixPath(*resolved.parts)
    if not str(normalized).startswith("/workspace"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Path traversal detected — path must stay within /workspace",
        )
    return str(normalized)


async def _wait_for_agent_bridge(
    container_name: str,
    timeout: float = 30.0,
    interval: float = 0.5,
) -> None:
    """Poll the agent-bridge WebSocket until it accepts connections.

    After docker.create_container() returns, the container is running but
    the WebSocket server inside may not have bound its port yet.  This
    readiness probe prevents the first message from hitting a
    ConnectionRefusedError.
    """
    deadline = asyncio.get_event_loop().time() + timeout
    last_error: Exception | None = None
    while asyncio.get_event_loop().time() < deadline:
        try:
            async with websockets.connect(
                f"ws://{container_name}:9100", open_timeout=2,
            ):
                logger.info("Agent-bridge ready on %s", container_name)
                return
        except Exception as exc:
            last_error = exc
            await asyncio.sleep(interval)
    raise TimeoutError(
        f"Agent-bridge on {container_name} not ready after {timeout}s: {last_error}"
    )


class CreateContainerRequest(BaseModel):
    session_id: str
    container_name: str
    user_id: str
    telegram_id: int
    env_vars: dict[str, str] = {}


class ExecCommandRequest(BaseModel):
    command: str
    env_vars: dict[str, str] = {}


class SendMessageRequest(BaseModel):
    text: str
    env_vars: dict[str, str] = {}


@router.post("/containers", status_code=status.HTTP_201_CREATED)
async def create_container(
    payload: CreateContainerRequest,
    docker: DockerClient = Depends(get_docker_client),
    _: None = Depends(verify_token),
) -> dict:
    """Create and start a new agent container for a user session."""
    try:
        container_id = await docker.create_container(
            container_name=payload.container_name,
            user_id=payload.user_id,
            env_vars=payload.env_vars,
            agent_image=settings.agent_image,
            workspace_base_path=settings.workspace_base_path,
            agent_network=settings.agent_network,
        )
        await _wait_for_agent_bridge(payload.container_name, timeout=30.0)
    except Exception as exc:
        logger.exception(
            "Failed to create container %s for user %s",
            payload.container_name,
            payload.user_id,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Container creation failed: {exc}",
        ) from exc
    return {"container_id": container_id, "container_name": payload.container_name}


@router.post("/containers/{container_id}/stop", status_code=status.HTTP_204_NO_CONTENT)
async def stop_container(
    container_id: str,
    docker: DockerClient = Depends(get_docker_client),
    _: None = Depends(verify_token),
) -> None:
    await docker.stop_container(container_id)


@router.post("/containers/{container_id}/restart", status_code=status.HTTP_204_NO_CONTENT)
async def restart_container(
    container_id: str,
    docker: DockerClient = Depends(get_docker_client),
    _: None = Depends(verify_token),
) -> None:
    await docker.restart_container(container_id)


@router.delete("/containers/{container_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_container(
    container_id: str,
    docker: DockerClient = Depends(get_docker_client),
    _: None = Depends(verify_token),
) -> None:
    await docker.remove_container(container_id, with_volume=True)


@router.post("/containers/{container_id}/exec")
async def exec_command(
    container_id: str,
    payload: ExecCommandRequest,
    docker: DockerClient = Depends(get_docker_client),
    _: None = Depends(verify_token),
) -> StreamingResponse:
    """Execute a shell command and stream output via SSE."""

    async def generate():
        try:
            async for line in docker.exec_command(container_id, payload.command):
                yield f"data: {json.dumps({'chunk': line})}\n\n"
        except Exception as exc:
            yield f"data: {json.dumps({'error': str(exc)})}\n\n"
        finally:
            yield "data: [DONE]\n\n"

    return StreamingResponse(generate(), media_type="text/event-stream")


@router.post("/containers/{container_id}/message")
async def send_message_to_agent(
    container_id: str,
    payload: SendMessageRequest,
    docker: DockerClient = Depends(get_docker_client),
    _: None = Depends(verify_token),
) -> StreamingResponse:
    """Forward a message to the agent bridge via WebSocket and stream the response."""
    # Resolve the container name which acts as DNS hostname on agent-net.
    container_name = await docker.get_container_name(container_id)

    async def generate():
        event_count = 0
        try:
            uri = f"ws://{container_name}:9100"
            logger.info("Connecting to agent-bridge at %s", uri)

            # Retry with exponential backoff for transient connection failures
            # (e.g. container just unpaused, bridge still rebinding port).
            max_retries = 3
            retry_delay = 0.5
            ws = None
            for attempt in range(max_retries):
                try:
                    ws = await websockets.connect(uri, open_timeout=10)
                    break
                except (OSError, websockets.exceptions.WebSocketException) as exc:
                    if attempt == max_retries - 1:
                        raise
                    logger.warning(
                        "WebSocket attempt %d/%d for %s: %s",
                        attempt + 1, max_retries, container_id, exc,
                    )
                    await asyncio.sleep(retry_delay)
                    retry_delay *= 2

            try:
                request_payload = json.dumps({
                    "method": "execute_prompt",
                    "params": {
                        "prompt": payload.text,
                        "env_vars": payload.env_vars,
                    },
                    "id": "1",
                })
                await ws.send(request_payload)

                # Read streaming JSON-RPC frames until we get done=true.
                async for raw_frame in ws:
                    frame = json.loads(raw_frame)
                    if frame.get("error"):
                        logger.warning(
                            "Agent-bridge error for container %s: %s",
                            container_id, frame["error"],
                        )
                        yield f"data: {json.dumps({'error': frame['error']})}\n\n"
                        break

                    # New structured event path (from SDK runner).
                    event = frame.get("event")
                    if event:
                        event_count += 1
                        yield f"data: {json.dumps({'event': event})}\n\n"

                    # Legacy chunk path (from old CLI runner / run_shell).
                    chunk = frame.get("chunk", "")
                    if chunk:
                        event_count += 1
                        yield f"data: {json.dumps({'chunk': chunk})}\n\n"

                    if frame.get("done", False):
                        break
            finally:
                await ws.close()
        except Exception as exc:
            logger.exception(
                "WebSocket error for container %s: %s", container_id, exc,
            )
            yield f"data: {json.dumps({'error': str(exc)})}\n\n"
        finally:
            logger.info(
                "Agent message stream ended for container %s: %d events",
                container_id, event_count,
            )
            yield "data: [DONE]\n\n"

    return StreamingResponse(generate(), media_type="text/event-stream")


@router.post("/containers/{container_id}/upload", status_code=status.HTTP_200_OK)
async def upload_file(
    container_id: str,
    request: Request,
    x_filename: str = Header(..., alias="X-Filename"),
    docker: DockerClient = Depends(get_docker_client),
    _: None = Depends(verify_token),
) -> dict:
    """Write an uploaded file into the container's /workspace directory."""
    safe_path = _validate_workspace_path(x_filename)
    file_content = await request.body()

    # Use docker exec to write the file using base64 to handle binary content.
    import base64
    encoded = base64.b64encode(file_content).decode("ascii")
    quoted_path = shlex.quote(safe_path)
    command = f"echo '{encoded}' | base64 -d > {quoted_path}"

    output_lines = []
    async for line in docker.exec_command(container_id, command):
        output_lines.append(line)

    return {"filename": x_filename, "size": len(file_content)}


@router.get("/containers/{container_id}/download/{file_path:path}")
async def download_file(
    container_id: str,
    file_path: str,
    docker: DockerClient = Depends(get_docker_client),
    _: None = Depends(verify_token),
) -> StreamingResponse:
    """Stream a file from the container's /workspace directory."""
    safe_path = _validate_workspace_path(file_path)

    async def stream():
        # Read file as base64 chunks via exec, then decode on the fly.
        import base64
        quoted_path = shlex.quote(safe_path)
        chunks = []
        async for line in docker.exec_command(
            container_id, f"base64 {quoted_path}"
        ):
            chunks.append(line)
        raw = base64.b64decode("".join(chunks))
        yield raw

    filename = file_path.split("/")[-1]
    return StreamingResponse(
        stream(),
        media_type="application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/containers/{container_id}/stats")
async def get_container_stats(
    container_id: str,
    docker: DockerClient = Depends(get_docker_client),
    _: None = Depends(verify_token),
) -> dict:
    return await docker.get_container_stats(container_id)


@router.get("/health")
async def health_check() -> dict:
    return {"status": "ok", "service": "container-manager"}
