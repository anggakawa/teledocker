FROM ubuntu:22.04

# Prevent apt from prompting for timezone/region during install.
ENV DEBIAN_FRONTEND=noninteractive
ENV TZ=UTC

RUN apt-get update && apt-get install -y \
    curl \
    git \
    build-essential \
    python3 \
    python3-pip \
    wget \
    unzip \
    jq \
    && rm -rf /var/lib/apt/lists/*

# Install uv to a shared location so both root and user 1000 can use it.
RUN curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR=/usr/local/bin sh

# Install Claude Code CLI via official standalone installer.
# Installs as root, then symlink to /usr/local/bin so the non-root
# user (uid 1000) can invoke it without modifying PATH.
RUN curl -fsSL https://claude.ai/install.sh | bash \
    && cp /root/.local/bin/claude /usr/local/bin/claude \
    && chmod 755 /usr/local/bin/claude

# Create non-root user (uid 1000) for all container operations.
RUN useradd --uid 1000 --create-home --shell /bin/bash chatops
RUN mkdir -p /workspace && chown 1000:1000 /workspace
RUN mkdir -p /opt/agent-bridge && chown 1000:1000 /opt/agent-bridge

# Switch to non-root user before installing the agent bridge so that
# uv installs Python and .venv under ~chatops (not /root).
USER 1000
COPY --chown=1000:1000 images/claude-agent/agent-bridge/ /opt/agent-bridge/
WORKDIR /opt/agent-bridge
RUN uv sync
WORKDIR /workspace
VOLUME ["/workspace"]

EXPOSE 9100

CMD ["uv", "run", "--directory", "/opt/agent-bridge", "python", "-m", "agent_bridge.main", "--port", "9100"]
