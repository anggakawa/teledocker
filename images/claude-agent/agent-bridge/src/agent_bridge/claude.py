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


class ClaudeCodeRunner:
    """Manages Claude Code subprocess invocation and output streaming."""

    def __init__(self):
        # Track whether we have an existing session to continue.
        self._has_session = False

    async def send_message(
        self, prompt: str, env_vars: dict[str, str]
    ) -> AsyncGenerator[str, None]:
        """Send a prompt to Claude Code and stream its output line by line.

        Args:
            prompt: The user's message to send to Claude Code.
            env_vars: Environment variables to inject (API keys, provider config).

        Yields:
            Lines of output from Claude Code as they arrive.
        """
        env = {**os.environ, **env_vars}

        # Use --continue to resume the previous conversation session.
        # On the very first message there is no session to continue.
        continue_flag = "--continue" if self._has_session else "--new"

        cmd = [
            _CLAUDE_BINARY,
            _SKIP_PERMISSIONS_FLAG,
            continue_flag,
            "--print",  # Print response to stdout instead of interactive mode.
            "--output-format", "stream-json",  # Stream JSON events line by line.
        ]

        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )

        # Send the prompt via stdin.
        stdout, stderr = await asyncio.wait_for(
            process.communicate(input=prompt.encode("utf-8")),
            timeout=300,  # 5-minute hard timeout for very long responses.
        )

        self._has_session = True

        if process.returncode != 0:
            error_text = stderr.decode("utf-8", errors="replace")
            logger.error("Claude Code exited with code %s: %s", process.returncode, error_text)
            yield f"Error: {error_text}"
            return

        # Stream each line of stdout back to the caller.
        for line in stdout.decode("utf-8", errors="replace").splitlines():
            if line.strip():
                yield line

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
