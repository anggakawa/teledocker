"""HTTP endpoints for container lifecycle and interaction operations."""

import json
import logging

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from container_manager.config import settings
from container_manager.docker_client import DockerClient

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


class CreateContainerRequest(BaseModel):
    session_id: str
    container_name: str
    user_id: str
    telegram_id: int
    env_vars: dict[str, str] = {}


class ExecCommandRequest(BaseModel):
    command: str


class SendMessageRequest(BaseModel):
    text: str


@router.post("/containers", status_code=status.HTTP_201_CREATED)
async def create_container(
    payload: CreateContainerRequest,
    docker: DockerClient = Depends(get_docker_client),
    _: None = Depends(verify_token),
) -> dict:
    """Create and start a new agent container for a user session."""
    container_id = await docker.create_container(
        container_name=payload.container_name,
        user_id=payload.user_id,
        env_vars=payload.env_vars,
        agent_image=settings.agent_image,
        workspace_base_path=settings.workspace_base_path,
    )
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
    import websockets

    async def generate():
        try:
            # The container name is used as hostname inside the Docker network.
            # We need to look up the container name from the container ID.
            # For simplicity, we use the container_id directly — in practice
            # the container is in NetworkMode=none so we'd use docker exec instead.
            # For the agent bridge communication, we use docker exec to relay.
            message_payload = json.dumps(
                {"method": "execute_prompt", "params": {"prompt": payload.text}, "id": "1"}
            )
            async for line in docker.exec_command(
                container_id,
                f"echo '{message_payload}' | uv run --directory /opt/agent-bridge python -c "
                "\"import sys,asyncio,json; from agent_bridge.claude import ClaudeCodeRunner; "
                "runner = ClaudeCodeRunner(); "
                "asyncio.run(runner.send_message(json.loads(sys.stdin.read())['params']['prompt'], {}))\"",
            ):
                yield f"data: {json.dumps({'chunk': line})}\n\n"
        except Exception as exc:
            yield f"data: {json.dumps({'error': str(exc)})}\n\n"
        finally:
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
    file_content = await request.body()

    # Use docker exec to write the file using base64 to handle binary content.
    import base64
    encoded = base64.b64encode(file_content).decode("ascii")
    command = f"echo '{encoded}' | base64 -d > /workspace/{x_filename}"

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

    async def stream():
        # Read file as base64 chunks via exec, then decode on the fly.
        import base64
        chunks = []
        async for line in docker.exec_command(
            container_id, f"base64 /workspace/{file_path}"
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
