"""JSON-RPC method handlers for the agent bridge WebSocket server.

Each handler corresponds to one JSON-RPC method name:
- execute_prompt: Send a message to Claude Code.
- run_shell: Execute a raw shell command.
- upload_file: Write bytes to /workspace.
- download_file: Read a file from /workspace as base64.
- health_check: Return CPU, RAM, disk usage.
"""

import base64
import logging
from collections.abc import AsyncGenerator
from pathlib import Path

import psutil

from agent_bridge.claude import ClaudeCodeRunner

logger = logging.getLogger(__name__)

_WORKSPACE = Path("/workspace")


def _validate_path(user_path: str) -> Path:
    """Resolve a user-supplied path and ensure it stays within /workspace.

    Returns the safe resolved Path. Raises ValueError on traversal attempts.
    """
    target = (_WORKSPACE / user_path).resolve()
    if not str(target).startswith(str(_WORKSPACE.resolve())):
        raise ValueError(f"Path traversal detected: {user_path}")
    return target


async def execute_prompt(
    params: dict, runner: ClaudeCodeRunner
) -> AsyncGenerator[str, None]:
    """Stream Claude Code's response to a user prompt."""
    prompt = params.get("prompt", "")
    env_vars = params.get("env_vars", {})

    async for line in runner.send_message(prompt, env_vars):
        yield line


async def run_shell(params: dict, runner: ClaudeCodeRunner) -> AsyncGenerator[str, None]:
    """Stream output from a raw shell command."""
    command = params.get("command", "echo 'No command provided'")

    async for line in runner.run_shell(command):
        yield line


async def upload_file(params: dict) -> dict:
    """Write a base64-encoded file to /workspace.

    Args:
        params: {"filename": str, "content_base64": str}

    Returns:
        {"success": True, "path": str, "size": int}
    """
    filename = params.get("filename", "upload")
    content_base64 = params.get("content_base64", "")

    file_bytes = base64.b64decode(content_base64)
    destination = _validate_path(filename)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(file_bytes)

    return {"success": True, "path": str(destination), "size": len(file_bytes)}


async def download_file(params: dict) -> dict:
    """Read a file from /workspace and return it as base64.

    Args:
        params: {"path": str}

    Returns:
        {"success": True, "content_base64": str, "size": int}

    Raises:
        FileNotFoundError: If the path doesn't exist under /workspace.
    """
    relative_path = params.get("path", "")
    file_path = _validate_path(relative_path)

    if not file_path.exists():
        raise FileNotFoundError(f"File not found: {file_path}")

    file_bytes = file_path.read_bytes()
    return {
        "success": True,
        "content_base64": base64.b64encode(file_bytes).decode("ascii"),
        "size": len(file_bytes),
    }


async def health_check() -> dict:
    """Return current resource usage of the container."""
    cpu_percent = psutil.cpu_percent(interval=0.1)
    memory = psutil.virtual_memory()
    disk = psutil.disk_usage("/workspace") if _WORKSPACE.exists() else psutil.disk_usage("/")

    return {
        "status": "ok",
        "cpu_percent": cpu_percent,
        "memory_used_mb": round(memory.used / 1024 / 1024, 1),
        "memory_total_mb": round(memory.total / 1024 / 1024, 1),
        "memory_percent": memory.percent,
        "disk_used_gb": round(disk.used / 1024 / 1024 / 1024, 2),
        "disk_total_gb": round(disk.total / 1024 / 1024 / 1024, 2),
        "disk_percent": disk.percent,
    }
