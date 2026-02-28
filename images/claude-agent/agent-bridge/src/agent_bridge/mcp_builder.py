"""Builds SDK-compatible mcp_servers dict from the registry and user env vars.

Takes the MCP_SERVER_REGISTRY definitions and the user's runtime env_vars,
filters to servers whose required_env_vars are present, resolves {PLACEHOLDER}
templates, and returns a dict ready for ClaudeAgentOptions(mcp_servers=...).

Also loads user-installed MCP servers from Claude Code's settings files
(written by `claude mcp add`) and merges them on top of registry servers.
"""

import json
import logging
from pathlib import Path
from typing import Any

from agent_bridge.mcp_config import (
    MCP_SERVER_REGISTRY,
    McpServerDefinition,
    McpTransportType,
)

logger = logging.getLogger(__name__)

# Claude Code settings files where `claude mcp add` writes mcpServers.
# Ordered by precedence: later files override earlier ones.
DEFAULT_SETTINGS_PATHS: list[Path] = [
    Path.home() / ".claude.json",           # local scope (default for `claude mcp add`)
    Path.home() / ".claude" / "settings.json",  # user scope
    Path("/workspace/.claude/settings.json"),   # project scope
]


def _resolve_templates(
    template: dict[str, str], env_vars: dict[str, str]
) -> dict[str, str]:
    """Replace {PLACEHOLDER} patterns in template values with actual env var values.

    Example:
        template = {"TOKEN": "{GITHUB_TOKEN}"}
        env_vars = {"GITHUB_TOKEN": "ghp_abc123"}
        result  = {"TOKEN": "ghp_abc123"}
    """
    resolved = {}
    for key, value in template.items():
        resolved[key] = value.format_map(env_vars)
    return resolved


def _build_stdio_config(
    definition: McpServerDefinition, env_vars: dict[str, str]
) -> dict[str, Any]:
    """Build an McpStdioServerConfig-compatible dict."""
    config: dict[str, Any] = {"command": definition.command}
    if definition.args:
        config["args"] = list(definition.args)
    if definition.env_template:
        config["env"] = _resolve_templates(definition.env_template, env_vars)
    return config


def _build_sse_config(
    definition: McpServerDefinition, env_vars: dict[str, str]
) -> dict[str, Any]:
    """Build an McpSSEServerConfig-compatible dict."""
    config: dict[str, Any] = {"type": "sse", "url": definition.url}
    if definition.headers_template:
        config["headers"] = _resolve_templates(definition.headers_template, env_vars)
    return config


def _build_http_config(
    definition: McpServerDefinition, env_vars: dict[str, str]
) -> dict[str, Any]:
    """Build an McpHttpServerConfig-compatible dict."""
    config: dict[str, Any] = {"type": "http", "url": definition.url}
    if definition.headers_template:
        config["headers"] = _resolve_templates(definition.headers_template, env_vars)
    return config


# Dispatch table keyed by transport type.
_BUILDERS: dict[McpTransportType, Any] = {
    McpTransportType.STDIO: _build_stdio_config,
    McpTransportType.SSE: _build_sse_config,
    McpTransportType.HTTP: _build_http_config,
}


def _build_from_registry(
    env_vars: dict[str, str],
    registry: list[McpServerDefinition],
) -> dict[str, Any]:
    """Build mcp_servers dict from registry definitions gated by env vars.

    Iterates the registry and includes only servers whose required_env_vars
    are all present and non-empty in env_vars.
    """
    servers: dict[str, Any] = {}

    for definition in registry:
        # Check all required env vars are present and non-empty.
        missing = [
            var
            for var in definition.required_env_vars
            if not env_vars.get(var)
        ]
        if missing:
            logger.debug(
                "Skipping MCP server '%s': missing env vars %s",
                definition.name,
                missing,
            )
            continue

        builder = _BUILDERS.get(definition.transport)
        if builder is None:
            logger.warning(
                "Unknown transport '%s' for MCP server '%s', skipping",
                definition.transport,
                definition.name,
            )
            continue

        servers[definition.name] = builder(definition, env_vars)
        logger.info("Activated MCP server '%s'", definition.name)

    return servers


def load_user_mcp_servers(
    settings_paths: list[Path] | None = None,
) -> dict[str, Any]:
    """Load MCP servers that the user installed via `claude mcp add`.

    Reads Claude Code settings files in order and extracts the mcpServers
    dict from each. Later files override earlier ones (project > user > local).

    Args:
        settings_paths: Override the default settings file locations (for testing).

    Returns:
        Merged dict of user-installed MCP server configs.
        Empty dict if no settings files exist or none contain mcpServers.
    """
    if settings_paths is None:
        settings_paths = DEFAULT_SETTINGS_PATHS

    merged: dict[str, Any] = {}

    for path in settings_paths:
        if not path.is_file():
            logger.debug("Settings file not found, skipping: %s", path)
            continue

        try:
            raw = path.read_text(encoding="utf-8")
            data = json.loads(raw)
        except (OSError, json.JSONDecodeError) as exc:
            logger.debug("Could not read settings file %s: %s", path, exc)
            continue

        if not isinstance(data, dict):
            logger.debug("Settings file %s is not a JSON object, skipping", path)
            continue

        mcp_servers = data.get("mcpServers", {})
        if not isinstance(mcp_servers, dict):
            logger.debug("mcpServers in %s is not a dict, skipping", path)
            continue

        if mcp_servers:
            logger.info(
                "Loaded %d user MCP server(s) from %s: %s",
                len(mcp_servers),
                path,
                list(mcp_servers.keys()),
            )
            merged.update(mcp_servers)

    return merged


def build_mcp_servers(
    env_vars: dict[str, str],
    registry: list[McpServerDefinition] | None = None,
    settings_paths: list[Path] | None = None,
) -> dict[str, Any]:
    """Build the mcp_servers dict for ClaudeAgentOptions.

    Merges two sources:
    1. Registry servers — admin-defined, gated by env vars.
    2. User-installed servers — from Claude Code settings files (`claude mcp add`).

    User servers override registry servers with the same name, because an
    explicit user installation should take precedence over admin defaults.

    Args:
        env_vars: User's environment variables (API keys, tokens, etc.).
        registry: Override the default MCP_SERVER_REGISTRY (useful for testing).
        settings_paths: Override the default settings file locations (for testing).

    Returns:
        Dict mapping server names to their SDK config dicts.
        Empty dict if no servers qualify.
    """
    if registry is None:
        registry = MCP_SERVER_REGISTRY

    # Registry servers are the base layer.
    servers = _build_from_registry(env_vars, registry)

    # User-installed servers overlay on top (can override registry entries).
    user_servers = load_user_mcp_servers(settings_paths)
    servers.update(user_servers)

    return servers
