"""Async HTTP client for the ChatOps API server.

All methods include the X-Service-Token header automatically.
SSE streaming endpoints return async generators of parsed data chunks.
"""

import json
import logging
from collections.abc import AsyncGenerator
from uuid import UUID

import httpx

from chatops_shared.schemas.session import SessionDTO
from chatops_shared.schemas.user import UserDTO

logger = logging.getLogger(__name__)


class ApiClient:
    """Wraps all api-server endpoints with typed method calls."""

    def __init__(self, base_url: str, service_token: str):
        self._base_url = base_url.rstrip("/")
        self._headers = {"X-Service-Token": service_token}

    def _client(self, timeout: float = 30.0) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=self._base_url,
            headers=self._headers,
            timeout=timeout,
        )

    # -----------------------------------------------------------------------
    # User operations
    # -----------------------------------------------------------------------

    async def register_user(
        self, telegram_id: int, telegram_username: str | None, display_name: str
    ) -> UserDTO:
        async with self._client() as client:
            response = await client.post(
                "/api/v1/users/register",
                json={
                    "telegram_id": telegram_id,
                    "telegram_username": telegram_username,
                    "display_name": display_name,
                },
            )
            response.raise_for_status()
            return UserDTO.model_validate(response.json())

    async def get_user(self, telegram_id: int) -> UserDTO | None:
        async with self._client() as client:
            response = await client.get(f"/api/v1/users/{telegram_id}")
            if response.status_code == 404:
                return None
            response.raise_for_status()
            return UserDTO.model_validate(response.json())

    async def approve_user(self, telegram_id: int) -> UserDTO:
        async with self._client() as client:
            response = await client.post(f"/api/v1/users/{telegram_id}/approve")
            response.raise_for_status()
            return UserDTO.model_validate(response.json())

    async def reject_user(self, telegram_id: int) -> None:
        async with self._client() as client:
            response = await client.post(f"/api/v1/users/{telegram_id}/reject")
            response.raise_for_status()

    async def revoke_user(self, telegram_id: int) -> UserDTO:
        async with self._client() as client:
            response = await client.post(f"/api/v1/users/{telegram_id}/revoke")
            response.raise_for_status()
            return UserDTO.model_validate(response.json())

    async def list_users(self, status_filter: str | None = None) -> list[UserDTO]:
        params = {}
        if status_filter:
            params["status"] = status_filter
        async with self._client() as client:
            response = await client.get("/api/v1/users", params=params)
            response.raise_for_status()
            return [UserDTO.model_validate(u) for u in response.json()]

    async def set_api_key(
        self, telegram_id: int, api_key: str, provider: str, base_url: str | None
    ) -> None:
        async with self._client() as client:
            response = await client.put(
                f"/api/v1/users/{telegram_id}/apikey",
                json={"api_key": api_key, "provider": provider, "base_url": base_url},
            )
            response.raise_for_status()

    async def update_provider(
        self, telegram_id: int, provider: str, base_url: str | None
    ) -> None:
        """Update provider config without touching the encrypted API key."""
        async with self._client() as client:
            response = await client.put(
                f"/api/v1/users/{telegram_id}/provider",
                json={"provider": provider, "base_url": base_url},
            )
            response.raise_for_status()

    async def remove_api_key(self, telegram_id: int) -> None:
        async with self._client() as client:
            response = await client.delete(f"/api/v1/users/{telegram_id}/apikey")
            response.raise_for_status()

    # -----------------------------------------------------------------------
    # Session operations
    # -----------------------------------------------------------------------

    async def create_session(
        self,
        user_id: UUID,
        telegram_id: int,
        agent_type: str = "claude-code",
        system_prompt: str | None = None,
    ) -> SessionDTO:
        async with self._client(timeout=60.0) as client:
            response = await client.post(
                "/api/v1/sessions",
                json={
                    "user_id": str(user_id),
                    "telegram_id": telegram_id,
                    "agent_type": agent_type,
                    "system_prompt": system_prompt,
                },
            )
            response.raise_for_status()
            return SessionDTO.model_validate(response.json())

    async def get_active_session_by_telegram_id(
        self, telegram_id: int
    ) -> SessionDTO | None:
        """Find active session for a Telegram user (survives bot restarts)."""
        async with self._client() as client:
            response = await client.get(
                "/api/v1/sessions/active",
                params={"telegram_id": telegram_id},
            )
            if response.status_code == 404:
                return None
            response.raise_for_status()
            return SessionDTO.model_validate(response.json())

    async def get_session(self, session_id: UUID) -> SessionDTO | None:
        async with self._client() as client:
            response = await client.get(f"/api/v1/sessions/{session_id}")
            if response.status_code == 404:
                return None
            response.raise_for_status()
            return SessionDTO.model_validate(response.json())

    async def stop_session(self, session_id: UUID) -> None:
        async with self._client() as client:
            response = await client.post(f"/api/v1/sessions/{session_id}/stop")
            response.raise_for_status()

    async def restart_session(self, session_id: UUID) -> None:
        async with self._client() as client:
            response = await client.post(f"/api/v1/sessions/{session_id}/restart")
            response.raise_for_status()

    async def destroy_session(self, session_id: UUID) -> None:
        async with self._client() as client:
            response = await client.delete(f"/api/v1/sessions/{session_id}")
            response.raise_for_status()

    # -----------------------------------------------------------------------
    # Streaming operations
    # -----------------------------------------------------------------------

    async def stream_exec(
        self, session_id: UUID, command: str
    ) -> AsyncGenerator[str, None]:
        """Execute a command and yield output chunks as they arrive."""
        async with httpx.AsyncClient(
            base_url=self._base_url, headers=self._headers, timeout=300.0
        ) as client:
            async with client.stream(
                "POST",
                f"/api/v1/sessions/{session_id}/exec",
                json={"command": command},
            ) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if line.startswith("data: "):
                        data = line[6:]
                        if data == "[DONE]":
                            break
                        try:
                            parsed = json.loads(data)
                            chunk = parsed.get("chunk", "")
                            if chunk:
                                yield chunk
                            error = parsed.get("error", "")
                            if error:
                                yield f"Error: {error}"
                        except json.JSONDecodeError:
                            yield data

    async def stream_message(
        self, session_id: UUID, text: str, telegram_msg_id: int | None = None
    ) -> AsyncGenerator[str, None]:
        """Send a message to the AI agent and yield response chunks."""
        async with httpx.AsyncClient(
            base_url=self._base_url, headers=self._headers, timeout=300.0
        ) as client:
            async with client.stream(
                "POST",
                f"/api/v1/sessions/{session_id}/message",
                json={"text": text, "telegram_msg_id": telegram_msg_id},
            ) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if line.startswith("data: "):
                        data = line[6:]
                        if data == "[DONE]":
                            break
                        try:
                            parsed = json.loads(data)
                            chunk = parsed.get("chunk", "")
                            if chunk:
                                yield chunk
                            error = parsed.get("error", "")
                            if error:
                                yield f"Error: {error}"
                        except json.JSONDecodeError:
                            yield data

    async def upload_file(
        self, session_id: UUID, filename: str, file_bytes: bytes
    ) -> dict:
        async with self._client(timeout=60.0) as client:
            response = await client.post(
                f"/api/v1/sessions/{session_id}/upload",
                content=file_bytes,
                headers={"Content-Type": "application/octet-stream", "X-Filename": filename},
            )
            response.raise_for_status()
            return response.json()

    async def download_file(self, session_id: UUID, file_path: str) -> bytes:
        async with self._client(timeout=60.0) as client:
            response = await client.get(
                f"/api/v1/sessions/{session_id}/download/{file_path}"
            )
            response.raise_for_status()
            return response.content
