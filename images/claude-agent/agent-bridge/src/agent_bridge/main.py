"""WebSocket JSON-RPC server — entry point for the agent bridge.

Protocol (new structured events):
  Client -> Server: {"method": "execute_prompt", "params": {...}, "id": "abc"}
  Server -> Client: {"id": "abc", "event": {...}, "done": false}  (structured)
  Server -> Client: {"id": "abc", "done": true}                   (final frame)
  Server -> Client: {"id": "abc", "error": "...", "done": true}   (on error)

Legacy protocol (run_shell, backward compat):
  Server -> Client: {"id": "abc", "chunk": "...", "done": false}  (streaming)

Non-streaming methods (upload_file, download_file, health_check) return a single
response frame with "done": true and a "result" key.
"""

import asyncio
import json
import logging
from collections.abc import AsyncGenerator

import websockets
from websockets.server import WebSocketServerProtocol

from agent_bridge.claude import ClaudeCodeRunner
from agent_bridge.handlers import (
    download_file,
    execute_prompt,
    health_check,
    run_shell,
    upload_file,
)
from agent_bridge.sdk_runner import ClaudeSDKRunner

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-5s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


async def handle_connection(websocket: WebSocketServerProtocol) -> None:
    """Process all JSON-RPC messages from a single WebSocket connection.

    Each connection gets its own SDK runner (persistent multi-turn session)
    and a legacy CLI runner (for raw shell commands).
    """
    sdk_runner = ClaudeSDKRunner()
    legacy_runner = ClaudeCodeRunner()
    logger.info("New connection from %s", websocket.remote_address)

    async for raw_message in websocket:
        try:
            request = json.loads(raw_message)
        except json.JSONDecodeError as exc:
            await websocket.send(json.dumps({"error": f"Invalid JSON: {exc}", "done": True}))
            continue

        method = request.get("method", "")
        params = request.get("params", {})
        request_id = request.get("id", "unknown")

        await dispatch_request(websocket, sdk_runner, legacy_runner, method, params, request_id)


async def dispatch_request(
    websocket: WebSocketServerProtocol,
    sdk_runner: ClaudeSDKRunner,
    legacy_runner: ClaudeCodeRunner,
    method: str,
    params: dict,
    request_id: str,
) -> None:
    """Route a JSON-RPC request to the appropriate handler."""
    try:
        if method == "execute_prompt":
            # New path: structured event streaming via SDK.
            await stream_event_response(
                websocket, request_id, execute_prompt(params, sdk_runner)
            )

        elif method == "run_shell":
            # Legacy path: plain text chunks.
            await stream_chunk_response(
                websocket, request_id, run_shell(params, legacy_runner)
            )

        elif method == "upload_file":
            result = await upload_file(params)
            await websocket.send(json.dumps({"id": request_id, "result": result, "done": True}))

        elif method == "download_file":
            result = await download_file(params)
            await websocket.send(json.dumps({"id": request_id, "result": result, "done": True}))

        elif method == "health_check":
            result = await health_check()
            await websocket.send(json.dumps({"id": request_id, "result": result, "done": True}))

        else:
            await websocket.send(
                json.dumps({"id": request_id, "error": f"Unknown method: {method}", "done": True})
            )

    except Exception as exc:
        logger.exception("Error handling method %s: %s", method, exc)
        await websocket.send(
            json.dumps({"id": request_id, "error": str(exc), "done": True})
        )


async def stream_event_response(
    websocket: WebSocketServerProtocol,
    request_id: str,
    generator: AsyncGenerator[dict, None],
) -> None:
    """Stream structured event dicts from the SDK runner as JSON-RPC frames.

    Each frame uses the "event" key (not "chunk") to distinguish from legacy.
    """
    async for event_dict in generator:
        await websocket.send(
            json.dumps({"id": request_id, "event": event_dict, "done": False})
        )
    await websocket.send(json.dumps({"id": request_id, "done": True}))


async def stream_chunk_response(
    websocket: WebSocketServerProtocol,
    request_id: str,
    generator: AsyncGenerator[str, None],
) -> None:
    """Stream plain text chunks (legacy protocol for run_shell)."""
    async for chunk in generator:
        await websocket.send(
            json.dumps({"id": request_id, "chunk": chunk, "done": False})
        )
    await websocket.send(json.dumps({"id": request_id, "done": True}))


async def main(port: int = 9100) -> None:
    logger.info("Agent bridge listening on ws://0.0.0.0:%s", port)
    async with websockets.serve(handle_connection, "0.0.0.0", port):
        await asyncio.Future()  # Run forever until cancelled.


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="ChatOps Agent Bridge WebSocket server")
    parser.add_argument("--port", type=int, default=9100)
    args = parser.parse_args()

    asyncio.run(main(port=args.port))
