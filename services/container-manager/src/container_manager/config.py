"""Container manager configuration loaded from environment variables."""

from chatops_shared.config import SharedSettings


class ContainerManagerSettings(SharedSettings):
    """Settings specific to the container manager service."""

    agent_image: str = "chatops/claude-agent:latest"
    max_containers: int = 20

    # Base path on the Docker host where workspace volumes are stored.
    workspace_base_path: str = "/workspaces"

    # Minutes of inactivity before a container is paused.
    idle_timeout_minutes: int = 30

    # URL of the API server (used by health monitor to update session status).
    api_server_url: str = "http://api-server:8000"

    # Docker network that user containers join so container-manager can
    # reach the agent-bridge WebSocket on port 9100.
    agent_network: str = "agent-net"


settings = ContainerManagerSettings()
