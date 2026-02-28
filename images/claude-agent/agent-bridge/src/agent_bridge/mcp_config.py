"""Typed registry of preconfigured MCP server definitions.

Adding a new MCP server = appending one McpServerDefinition to MCP_SERVER_REGISTRY.
Servers whose required_env_vars are missing at runtime are silently skipped.
"""

from dataclasses import dataclass, field
from enum import Enum


class McpTransportType(Enum):
    """Transport protocol for MCP server communication."""

    STDIO = "stdio"
    SSE = "sse"
    HTTP = "http"


@dataclass(frozen=True)
class McpServerDefinition:
    """Declarative specification for a preconfigured MCP server.

    Attributes:
        name: Unique identifier used as the key in the SDK's mcp_servers dict.
        transport: Which protocol to use (stdio, sse, http).
        command: Executable for stdio servers (e.g. "npx").
        args: Arguments for stdio servers.
        url: Endpoint for sse/http servers.
        headers_template: Header templates with {PLACEHOLDER} for env var substitution.
        env_template: Environment variable templates for stdio servers.
            Keys are env var names passed to the subprocess, values are
            either literal strings or {PLACEHOLDER} patterns resolved from
            the user's env_vars at runtime.
        required_env_vars: List of env var names that must be present in the
            user's env_vars for this server to activate. If any are missing
            or empty, the server is silently skipped.
        description: Human-readable purpose of this server.
    """

    name: str
    transport: McpTransportType
    command: str | None = None
    args: tuple[str, ...] = ()
    url: str | None = None
    headers_template: dict[str, str] = field(default_factory=dict)
    env_template: dict[str, str] = field(default_factory=dict)
    required_env_vars: tuple[str, ...] = ()
    description: str = ""


MCP_SERVER_REGISTRY: list[McpServerDefinition] = [
    McpServerDefinition(
        name="github",
        transport=McpTransportType.STDIO,
        command="npx",
        args=("-y", "@modelcontextprotocol/server-github"),
        env_template={"GITHUB_PERSONAL_ACCESS_TOKEN": "{GITHUB_TOKEN}"},
        required_env_vars=("GITHUB_TOKEN",),
        description="GitHub API access: repos, issues, PRs, files",
    ),
]
