"""Tests for MCP server registry definitions.

Validates structural invariants: unique names, required fields per transport,
and frozen dataclass immutability.
"""

import pytest
from agent_bridge.mcp_config import (
    MCP_SERVER_REGISTRY,
    McpServerDefinition,
    McpTransportType,
)


class TestRegistryIntegrity:
    """Structural validation of MCP_SERVER_REGISTRY entries."""

    def test_registry_has_entries(self):
        """Registry should not be empty."""
        assert len(MCP_SERVER_REGISTRY) > 0

    def test_all_names_unique(self):
        """Server names must be unique since they become dict keys."""
        names = [d.name for d in MCP_SERVER_REGISTRY]
        assert len(names) == len(set(names)), f"Duplicate names: {names}"

    def test_stdio_definitions_have_command(self):
        """Stdio servers must specify a command to execute."""
        for definition in MCP_SERVER_REGISTRY:
            if definition.transport == McpTransportType.STDIO:
                assert definition.command, (
                    f"Stdio server '{definition.name}' missing command"
                )

    def test_sse_definitions_have_url(self):
        """SSE servers must specify a URL endpoint."""
        for definition in MCP_SERVER_REGISTRY:
            if definition.transport == McpTransportType.SSE:
                assert definition.url, (
                    f"SSE server '{definition.name}' missing url"
                )

    def test_http_definitions_have_url(self):
        """HTTP servers must specify a URL endpoint."""
        for definition in MCP_SERVER_REGISTRY:
            if definition.transport == McpTransportType.HTTP:
                assert definition.url, (
                    f"HTTP server '{definition.name}' missing url"
                )

    def test_all_definitions_have_description(self):
        """Every server should have a description for discoverability."""
        for definition in MCP_SERVER_REGISTRY:
            assert definition.description, (
                f"Server '{definition.name}' missing description"
            )


class TestMcpServerDefinitionImmutability:
    """Frozen dataclass should prevent accidental mutation."""

    def test_frozen_prevents_field_assignment(self):
        definition = McpServerDefinition(
            name="test",
            transport=McpTransportType.STDIO,
            command="echo",
        )
        with pytest.raises(AttributeError):
            definition.name = "mutated"

    def test_frozen_prevents_command_assignment(self):
        definition = McpServerDefinition(
            name="test",
            transport=McpTransportType.STDIO,
            command="echo",
        )
        with pytest.raises(AttributeError):
            definition.command = "rm"


class TestGithubServerDefinition:
    """Specific tests for the GitHub MCP server entry."""

    def _get_github(self) -> McpServerDefinition:
        matches = [d for d in MCP_SERVER_REGISTRY if d.name == "github"]
        assert len(matches) == 1, "Expected exactly one 'github' entry"
        return matches[0]

    def test_github_uses_stdio_transport(self):
        github = self._get_github()
        assert github.transport == McpTransportType.STDIO

    def test_github_uses_npx(self):
        github = self._get_github()
        assert github.command == "npx"

    def test_github_requires_token(self):
        github = self._get_github()
        assert "GITHUB_TOKEN" in github.required_env_vars

    def test_github_passes_token_via_env_template(self):
        github = self._get_github()
        assert "GITHUB_PERSONAL_ACCESS_TOKEN" in github.env_template
        assert github.env_template["GITHUB_PERSONAL_ACCESS_TOKEN"] == "{GITHUB_TOKEN}"
