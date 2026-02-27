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
    nodejs \
    npm \
    wget \
    unzip \
    jq \
    && rm -rf /var/lib/apt/lists/*

# Install uv for the agent bridge Python environment.
RUN curl -LsSf https://astral.sh/uv/install.sh | sh
ENV PATH="/root/.local/bin:$PATH"

# Install Claude Code CLI.
# NOTE: The real install URL is not publicly available yet.
# Uncomment the line below when the official installer is released:
# RUN curl -fsSL https://claude.ai/install.sh | bash
# ENV PATH="/root/.claude/bin:$PATH"
#
# For development/testing without the real binary, install a stub:
RUN printf '#!/bin/bash\necho "Claude Code stub: $@"\ncat -\n' > /usr/local/bin/claude \
    && chmod +x /usr/local/bin/claude

# Create non-root user (uid 1000) for all container operations.
RUN useradd --uid 1000 --create-home --shell /bin/bash chatops
RUN mkdir -p /workspace && chown 1000:1000 /workspace
RUN mkdir -p /opt/agent-bridge && chown 1000:1000 /opt/agent-bridge

# Copy and install the agent bridge as the non-root user.
COPY --chown=1000:1000 agent-bridge/ /opt/agent-bridge/
WORKDIR /opt/agent-bridge
RUN uv sync --frozen

USER 1000
WORKDIR /workspace
VOLUME ["/workspace"]

EXPOSE 9100

CMD ["uv", "run", "--directory", "/opt/agent-bridge", "python", "-m", "agent_bridge.main", "--port", "9100"]
