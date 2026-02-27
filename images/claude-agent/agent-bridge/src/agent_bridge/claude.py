"""Claude Code CLI wrapper for headless execution inside containers.

Claude Code CLI is invoked via asyncio subprocess with --dangerously-skip-permissions
so it can execute tool calls (file writes, shell commands) without interactive
prompts. This is safe because each user's container is fully isolated.

Conversation history is managed by Claude Code's own session system when
the --continue flag is used between messages.
"""

import asyncio
import logging
import os
from collections.abc import AsyncGenerator

logger = logging.getLogger(__name__)

# Path where Claude Code binary is installed in the agent container.
_CLAUDE_BINARY = "claude"

# Flag that allows Claude Code to execute tools without prompting.
_SKIP_PERMISSIONS_FLAG = "--dangerously-skip-permissions"

# Hard timeout for very long responses (5 minutes).
_PROCESS_TIMEOUT_SECONDS = 300


class ClaudeCodeRunner:
    """Manages Claude Code subprocess invocation and output streaming."""

    def __init__(self):
        # Track whether we have an existing session to continue.
        self._has_session = False

    async def send_message(
        self, prompt: str, env_vars: dict[str, str]
    ) -> AsyncGenerator[str, None]:
        """Send a prompt to Claude Code and stream its output line by line.

        Yields lines of output as the subprocess produces them (true streaming),
        rather than buffering the entire response before yielding.

        Args:
            prompt: The user's message to send to Claude Code.
            env_vars: Environment variables to inject (API keys, provider config).

        Yields:
            Lines of output from Claude Code as they arrive.
        """
        env = {**os.environ, **env_vars}

        cmd = [
            _CLAUDE_BINARY,
            _SKIP_PERMISSIONS_FLAG,
            "--print",  # Print response to stdout instead of interactive mode.
            "--output-format", "text",  # Plain text output, one line at a time.
        ]

        # Use --continue to resume the previous conversation session.
        # On the very first message, omit it — CLI starts a new session by default.
        if self._has_session:
            cmd.append("--continue")

        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )

        # Send the prompt via stdin and close to signal EOF.
        process.stdin.write(prompt.encode("utf-8"))
        await process.stdin.drain()
        process.stdin.close()
        await process.stdin.wait_closed()

        self._has_session = True

        # Stream stdout line-by-line as the subprocess produces output.
        try:
            async with asyncio.timeout(_PROCESS_TIMEOUT_SECONDS):
                async for raw_line in process.stdout:
                    line = raw_line.decode("utf-8", errors="replace").rstrip("\n")
                    if line.strip():
                        yield line
        except TimeoutError:
            process.kill()
            yield "Error: Claude Code process timed out after 5 minutes"
            return

        await process.wait()

        # Report stderr only if the process failed.
        if process.returncode != 0:
            stderr_bytes = await process.stderr.read()
            error_text = stderr_bytes.decode("utf-8", errors="replace").strip()
            logger.error("Claude Code exited with code %s: %s", process.returncode, error_text)
            yield f"Error: {error_text}"

    async def run_shell(self, command: str) -> AsyncGenerator[str, None]:
        """Run an arbitrary shell command and stream stdout/stderr.

        Args:
            command: Shell command string to execute.

        Yields:
            Lines of combined stdout and stderr output.
        """
        process = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd="/workspace",
        )

        assert process.stdout is not None
        async for line in process.stdout:
            yield line.decode("utf-8", errors="replace").rstrip("\n")

        await process.wait()
