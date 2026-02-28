"""Tests for MCP builder — env var gating, template resolution, config building.

Uses custom registries injected via the registry parameter to keep tests
isolated from actual MCP_SERVER_REGISTRY contents.
"""

import json
from pathlib import Path

from agent_bridge.mcp_builder import build_mcp_servers, load_user_mcp_servers
from agent_bridge.mcp_config import McpServerDefinition, McpTransportType


def _make_stdio_def(
    name: str = "test-stdio",
    command: str = "echo",
    args: tuple[str, ...] = ("hello",),
    env_template: dict[str, str] | None = None,
    required_env_vars: tuple[str, ...] = (),
) -> McpServerDefinition:
    """Helper to create a stdio server definition for testing."""
    return McpServerDefinition(
        name=name,
        transport=McpTransportType.STDIO,
        command=command,
        args=args,
        env_template=env_template or {},
        required_env_vars=required_env_vars,
        description="test stdio server",
    )


def _make_sse_def(
    name: str = "test-sse",
    url: str = "http://localhost:8080/sse",
    headers_template: dict[str, str] | None = None,
    required_env_vars: tuple[str, ...] = (),
) -> McpServerDefinition:
    """Helper to create an SSE server definition for testing."""
    return McpServerDefinition(
        name=name,
        transport=McpTransportType.SSE,
        url=url,
        headers_template=headers_template or {},
        required_env_vars=required_env_vars,
        description="test sse server",
    )


def _make_http_def(
    name: str = "test-http",
    url: str = "http://localhost:9090/api",
    headers_template: dict[str, str] | None = None,
    required_env_vars: tuple[str, ...] = (),
) -> McpServerDefinition:
    """Helper to create an HTTP server definition for testing."""
    return McpServerDefinition(
        name=name,
        transport=McpTransportType.HTTP,
        url=url,
        headers_template=headers_template or {},
        required_env_vars=required_env_vars,
        description="test http server",
    )


class TestEnvVarGating:
    """Servers should only activate when all required env vars are present."""

    def test_server_activated_when_env_vars_present(self):
        registry = [_make_stdio_def(required_env_vars=("MY_TOKEN",))]
        result = build_mcp_servers({"MY_TOKEN": "secret"}, registry=registry)
        assert "test-stdio" in result

    def test_server_skipped_when_env_var_missing(self):
        registry = [_make_stdio_def(required_env_vars=("MY_TOKEN",))]
        result = build_mcp_servers({}, registry=registry, settings_paths=[])
        assert result == {}

    def test_server_skipped_when_env_var_empty(self):
        registry = [_make_stdio_def(required_env_vars=("MY_TOKEN",))]
        result = build_mcp_servers({"MY_TOKEN": ""}, registry=registry, settings_paths=[])
        assert result == {}

    def test_server_skipped_when_one_of_multiple_env_vars_missing(self):
        registry = [_make_stdio_def(required_env_vars=("TOKEN_A", "TOKEN_B"))]
        result = build_mcp_servers({"TOKEN_A": "val"}, registry=registry, settings_paths=[])
        assert result == {}

    def test_server_activated_when_all_multiple_env_vars_present(self):
        registry = [_make_stdio_def(required_env_vars=("TOKEN_A", "TOKEN_B"))]
        result = build_mcp_servers(
            {"TOKEN_A": "a", "TOKEN_B": "b"}, registry=registry
        )
        assert "test-stdio" in result

    def test_no_required_env_vars_always_activates(self):
        """Servers with empty required_env_vars should always activate."""
        registry = [_make_stdio_def(required_env_vars=())]
        result = build_mcp_servers({}, registry=registry)
        assert "test-stdio" in result


class TestTemplateResolution:
    """Env and header templates should resolve {PLACEHOLDER} patterns."""

    def test_env_template_resolved(self):
        registry = [
            _make_stdio_def(
                env_template={"UPSTREAM_TOKEN": "{MY_TOKEN}"},
                required_env_vars=("MY_TOKEN",),
            )
        ]
        result = build_mcp_servers({"MY_TOKEN": "secret123"}, registry=registry)
        assert result["test-stdio"]["env"]["UPSTREAM_TOKEN"] == "secret123"

    def test_multiple_env_templates_resolved(self):
        registry = [
            _make_stdio_def(
                env_template={
                    "TOKEN": "{MY_TOKEN}",
                    "USER": "{MY_USER}",
                },
                required_env_vars=("MY_TOKEN", "MY_USER"),
            )
        ]
        result = build_mcp_servers(
            {"MY_TOKEN": "tok", "MY_USER": "usr"}, registry=registry
        )
        assert result["test-stdio"]["env"]["TOKEN"] == "tok"
        assert result["test-stdio"]["env"]["USER"] == "usr"

    def test_header_template_resolved_for_sse(self):
        registry = [
            _make_sse_def(
                headers_template={"Authorization": "Bearer {API_KEY}"},
                required_env_vars=("API_KEY",),
            )
        ]
        result = build_mcp_servers({"API_KEY": "key123"}, registry=registry)
        assert result["test-sse"]["headers"]["Authorization"] == "Bearer key123"

    def test_header_template_resolved_for_http(self):
        registry = [
            _make_http_def(
                headers_template={"X-Token": "{SECRET}"},
                required_env_vars=("SECRET",),
            )
        ]
        result = build_mcp_servers({"SECRET": "shhh"}, registry=registry)
        assert result["test-http"]["headers"]["X-Token"] == "shhh"


class TestStdioConfigBuilding:
    """Stdio servers should produce correct config dicts."""

    def test_basic_stdio_config(self):
        registry = [_make_stdio_def(command="npx", args=("-y", "some-pkg"))]
        result = build_mcp_servers({}, registry=registry)
        config = result["test-stdio"]
        assert config["command"] == "npx"
        assert config["args"] == ["-y", "some-pkg"]

    def test_stdio_without_args(self):
        registry = [_make_stdio_def(args=())]
        result = build_mcp_servers({}, registry=registry)
        config = result["test-stdio"]
        assert "args" not in config

    def test_stdio_without_env_template(self):
        registry = [_make_stdio_def(env_template={})]
        result = build_mcp_servers({}, registry=registry)
        config = result["test-stdio"]
        assert "env" not in config

    def test_stdio_does_not_include_type_key(self):
        """Stdio configs should not have a 'type' key (it's the default)."""
        registry = [_make_stdio_def()]
        result = build_mcp_servers({}, registry=registry)
        assert "type" not in result["test-stdio"]


class TestSseConfigBuilding:
    """SSE servers should produce correct config dicts."""

    def test_basic_sse_config(self):
        registry = [_make_sse_def(url="http://example.com/sse")]
        result = build_mcp_servers({}, registry=registry)
        config = result["test-sse"]
        assert config["type"] == "sse"
        assert config["url"] == "http://example.com/sse"

    def test_sse_without_headers(self):
        registry = [_make_sse_def(headers_template={})]
        result = build_mcp_servers({}, registry=registry)
        assert "headers" not in result["test-sse"]


class TestHttpConfigBuilding:
    """HTTP servers should produce correct config dicts."""

    def test_basic_http_config(self):
        registry = [_make_http_def(url="http://example.com/api")]
        result = build_mcp_servers({}, registry=registry)
        config = result["test-http"]
        assert config["type"] == "http"
        assert config["url"] == "http://example.com/api"

    def test_http_without_headers(self):
        registry = [_make_http_def(headers_template={})]
        result = build_mcp_servers({}, registry=registry)
        assert "headers" not in result["test-http"]


class TestMixedActivation:
    """Multiple servers with different activation requirements."""

    def test_only_qualifying_servers_included(self):
        registry = [
            _make_stdio_def(name="server-a", required_env_vars=("TOKEN_A",)),
            _make_stdio_def(name="server-b", required_env_vars=("TOKEN_B",)),
            _make_sse_def(name="server-c", required_env_vars=("TOKEN_C",)),
        ]
        result = build_mcp_servers({"TOKEN_A": "a", "TOKEN_C": "c"}, registry=registry)
        assert "server-a" in result
        assert "server-b" not in result
        assert "server-c" in result

    def test_empty_registry_returns_empty_dict(self):
        result = build_mcp_servers({"KEY": "val"}, registry=[], settings_paths=[])
        assert result == {}

    def test_default_registry_used_when_none(self):
        """When registry is None, should use the real MCP_SERVER_REGISTRY."""
        # Just verify it runs without error (real registry may or may not
        # produce results depending on env vars passed).
        result = build_mcp_servers({})
        assert isinstance(result, dict)


def _write_settings(path: Path, data: dict) -> None:
    """Helper to write a JSON settings file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


class TestLoadUserMcpServers:
    """Loading MCP servers from Claude Code settings files."""

    def test_loads_servers_from_single_settings_file(self, tmp_path: Path):
        settings_file = tmp_path / "settings.json"
        _write_settings(settings_file, {
            "mcpServers": {
                "my-server": {"command": "npx", "args": ["-y", "my-pkg"]},
            },
        })

        result = load_user_mcp_servers(settings_paths=[settings_file])

        assert "my-server" in result
        assert result["my-server"]["command"] == "npx"
        assert result["my-server"]["args"] == ["-y", "my-pkg"]

    def test_missing_file_returns_empty_dict(self, tmp_path: Path):
        nonexistent = tmp_path / "does-not-exist.json"

        result = load_user_mcp_servers(settings_paths=[nonexistent])

        assert result == {}

    def test_malformed_json_returns_empty_dict(self, tmp_path: Path):
        bad_file = tmp_path / "broken.json"
        bad_file.write_text("{not valid json!!!", encoding="utf-8")

        result = load_user_mcp_servers(settings_paths=[bad_file])

        assert result == {}

    def test_missing_mcp_servers_key_returns_empty_dict(self, tmp_path: Path):
        settings_file = tmp_path / "settings.json"
        _write_settings(settings_file, {"someOtherKey": "value"})

        result = load_user_mcp_servers(settings_paths=[settings_file])

        assert result == {}

    def test_mcp_servers_not_a_dict_returns_empty(self, tmp_path: Path):
        """If mcpServers is not a dict (e.g. a list), skip it."""
        settings_file = tmp_path / "settings.json"
        _write_settings(settings_file, {"mcpServers": ["not", "a", "dict"]})

        result = load_user_mcp_servers(settings_paths=[settings_file])

        assert result == {}

    def test_later_files_override_earlier(self, tmp_path: Path):
        """Project scope should override local scope for same server name."""
        local_file = tmp_path / "local.json"
        project_file = tmp_path / "project.json"

        _write_settings(local_file, {
            "mcpServers": {
                "shared-name": {"command": "old-cmd", "args": ["v1"]},
                "local-only": {"command": "local-cmd"},
            },
        })
        _write_settings(project_file, {
            "mcpServers": {
                "shared-name": {"command": "new-cmd", "args": ["v2"]},
                "project-only": {"command": "project-cmd"},
            },
        })

        result = load_user_mcp_servers(
            settings_paths=[local_file, project_file]
        )

        # shared-name overridden by project file
        assert result["shared-name"]["command"] == "new-cmd"
        assert result["shared-name"]["args"] == ["v2"]
        # unique servers from both files present
        assert result["local-only"]["command"] == "local-cmd"
        assert result["project-only"]["command"] == "project-cmd"

    def test_file_not_a_json_object_skipped(self, tmp_path: Path):
        """A settings file containing a JSON array should be skipped."""
        array_file = tmp_path / "array.json"
        array_file.write_text('["not", "an", "object"]', encoding="utf-8")

        result = load_user_mcp_servers(settings_paths=[array_file])

        assert result == {}

    def test_empty_mcp_servers_returns_empty(self, tmp_path: Path):
        settings_file = tmp_path / "settings.json"
        _write_settings(settings_file, {"mcpServers": {}})

        result = load_user_mcp_servers(settings_paths=[settings_file])

        assert result == {}

    def test_no_settings_paths_returns_empty(self):
        """Passing an empty list of paths should return empty dict."""
        result = load_user_mcp_servers(settings_paths=[])

        assert result == {}


class TestRegistryAndUserMerge:
    """Registry servers and user-installed servers should merge correctly."""

    def test_user_server_added_alongside_registry(self, tmp_path: Path):
        """User servers and registry servers coexist."""
        registry = [_make_stdio_def(name="registry-server")]
        settings_file = tmp_path / "settings.json"
        _write_settings(settings_file, {
            "mcpServers": {
                "user-server": {"command": "user-cmd"},
            },
        })

        result = build_mcp_servers(
            env_vars={},
            registry=registry,
            settings_paths=[settings_file],
        )

        assert "registry-server" in result
        assert "user-server" in result
        assert result["user-server"]["command"] == "user-cmd"

    def test_user_server_overrides_registry_with_same_name(self, tmp_path: Path):
        """A user-installed server with the same name replaces registry entry."""
        registry = [_make_stdio_def(name="github", command="registry-cmd")]
        settings_file = tmp_path / "settings.json"
        _write_settings(settings_file, {
            "mcpServers": {
                "github": {"command": "user-cmd", "args": ["--custom"]},
            },
        })

        result = build_mcp_servers(
            env_vars={},
            registry=registry,
            settings_paths=[settings_file],
        )

        assert result["github"]["command"] == "user-cmd"
        assert result["github"]["args"] == ["--custom"]

    def test_no_settings_files_returns_only_registry(self, tmp_path: Path):
        """When no settings files exist, only registry servers are returned."""
        registry = [_make_stdio_def(name="only-registry")]
        nonexistent = tmp_path / "nope.json"

        result = build_mcp_servers(
            env_vars={},
            registry=registry,
            settings_paths=[nonexistent],
        )

        assert list(result.keys()) == ["only-registry"]

    def test_empty_registry_returns_only_user_servers(self, tmp_path: Path):
        """When registry is empty, only user-installed servers are returned."""
        settings_file = tmp_path / "settings.json"
        _write_settings(settings_file, {
            "mcpServers": {
                "user-only": {"command": "some-cmd"},
            },
        })

        result = build_mcp_servers(
            env_vars={},
            registry=[],
            settings_paths=[settings_file],
        )

        assert list(result.keys()) == ["user-only"]
