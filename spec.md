# ChatOps AI Bridge — MVP Technical Specification

> **Version:** 1.0.0-mvp · **Date:** February 2026 · **Author:** Angga · **Status:** MVP Specification

---

## 1. MVP Scope & Boundaries

### 1.1 What's In

- Telegram Bot as the sole chat interface
- Per-user Docker container provisioning with Claude Code pre-installed
- Interactive terminal access: users send commands, receive stdout/stderr in chat
- File exchange: upload files to container, download files from container
- Session persistence: containers survive bot restarts, resume on next message
- Basic user management: registration, API key storage, usage tracking
- Container lifecycle commands: start, stop, restart, status

### 1.2 What's Out (Post-MVP)

- WhatsApp, Discord, Slack adapters
- Multi-agent switching (Aider, Open Interpreter)
- Team/shared container sessions
- Web-based admin dashboard
- Kubernetes deployment (Docker Compose only for MVP)
- Billing/payment system

---

## 2. System Architecture

### 2.1 Component Overview

The MVP consists of five deployable services orchestrated via Docker Compose:

| Service | Technology | Responsibility |
|---------|-----------|----------------|
| `telegram-bot` | Python + python-telegram-bot | Telegram Bot API, message routing, command parsing, response formatting |
| `api-server` | Python + FastAPI | REST API, session management, user auth, business logic orchestration |
| `container-manager` | Python + docker-py (aiodocker) | Docker container lifecycle, health monitoring, resource management |
| `postgres` | PostgreSQL 16 | User data, sessions, audit logs, API key storage (encrypted) |
| `redis` | Redis 7 | Session cache, pub/sub for real-time message relay, rate limiting |

### 2.2 Message Flow (User → Container → User)

Every interaction follows this exact sequence:

1. User sends message/command/file in Telegram
2. `telegram-bot` receives webhook update from Telegram API
3. `telegram-bot` calls `api-server` REST endpoint with normalized payload
4. `api-server` authenticates user, resolves session, determines action type
5. `api-server` calls `container-manager` to execute command in the user's container
6. `container-manager` runs `docker exec` or streams output via aiodocker attach API
7. Output streams back through `api-server` → Redis pub/sub → `telegram-bot`
8. `telegram-bot` formats and sends response to user (chunked if > 4096 chars)

### 2.3 Architecture Diagram

```
┌───────────────┐     ┌────────────────┐     ┌──────────────────┐
│  Telegram     │────▶│ telegram-bot   │────▶│   api-server     │
│  User Chat    │     │ (ptb)          │     │   (FastAPI)      │
└───────────────┘     └────────────────┘     └───────┬──────────┘
                                                     │
                       ┌─────────────────────────────┤
                       │                             │
                 ┌─────┴─────────┐     ┌─────────────┴───┐
                 │ container-mgr │     │  PostgreSQL +    │
                 │ (aiodocker)   │     │  Redis           │
                 └─────┬─────────┘     └─────────────────┘
                       │
              ┌────────┴──────────────────────────┐
              │  Docker Host (via socket)          │
              │  ┌─────────┐  ┌─────────┐  ...    │
              │  │ user-001│  │ user-002│         │
              │  │ Claude  │  │ Claude  │         │
              │  │ Code    │  │ Code    │         │
              │  └─────────┘  └─────────┘         │
              └───────────────────────────────────┘
```

---

## 3. Technology Stack

| Layer | Technology | Why This Choice |
|-------|-----------|-----------------|
| Language | Python 3.12+ | Your primary language, async-first with asyncio, rich ecosystem for Docker/Telegram/web |
| Package Manager | uv | Blazing fast dependency resolution, lockfile support, replaces pip/pip-tools/virtualenv |
| Telegram SDK | python-telegram-bot v21+ | Async-native, mature, excellent docs, built-in conversation handlers and job queue |
| HTTP Framework | FastAPI + Uvicorn | Async, auto-generated OpenAPI docs, Pydantic validation, dependency injection, SSE support |
| Docker Client | aiodocker | Async Docker API client, native asyncio, stream support for exec/attach |
| ORM / Database | SQLAlchemy 2.0 + asyncpg + Alembic | Async ORM with type hints, asyncpg for high-perf PostgreSQL, Alembic for migrations |
| Cache / PubSub | Redis 7 (redis-py async) | Session cache, message relay pub/sub, rate limiting via sorted sets |
| Encryption | cryptography (Fernet / AES-256-GCM) | Industry-standard Python crypto library. For API key encryption at rest |
| Validation | Pydantic v2 | Native FastAPI integration, fast validation, settings management via `pydantic-settings` |
| Container Image | Ubuntu 22.04 + Claude Code | Stable base, Claude Code pre-installed, dev tools included |
| Deployment | Docker Compose | Single-file deployment, easy to manage for MVP, portable |
| Reverse Proxy | Caddy v2 | Automatic HTTPS, zero-config TLS, simple Caddyfile |

---

## 4. Database Schema

### 4.1 Users Table

| Column | Type | Nullable | Description |
|--------|------|----------|-------------|
| `id` | UUID | NO | Primary key (`gen_random_uuid`) |
| `telegram_id` | BIGINT | NO | Telegram user ID (unique index) |
| `telegram_username` | VARCHAR(255) | YES | Telegram @username for display |
| `display_name` | VARCHAR(255) | NO | User's full name from Telegram |
| `role` | ENUM | NO | `admin` \| `user` \| `guest` (default: `guest`) |
| `is_approved` | BOOLEAN | NO | Whether user has been approved by admin (default: `false`) |
| `api_key_encrypted` | BYTEA | YES | AES-256-GCM encrypted Anthropic API key |
| `api_key_iv` | BYTEA | YES | Initialization vector for decryption |
| `provider_config` | JSONB | YES | Provider settings: `{provider: "anthropic"|"openrouter"|"custom", base_url?: string}` |
| `is_active` | BOOLEAN | NO | Whether user can access the system (default: `true`) |
| `max_containers` | INT | NO | Max simultaneous containers (default: `1`) |
| `created_at` | TIMESTAMPTZ | NO | Registration timestamp |
| `updated_at` | TIMESTAMPTZ | NO | Last update timestamp |

### 4.2 Sessions Table

| Column | Type | Nullable | Description |
|--------|------|----------|-------------|
| `id` | UUID | NO | Primary key |
| `user_id` | UUID (FK) | NO | References `users.id` |
| `container_id` | VARCHAR(64) | YES | Docker container ID (null if not yet created) |
| `container_name` | VARCHAR(255) | NO | Human-readable name (`chatops-{user_tg_id}-{short_uuid}`) |
| `status` | ENUM | NO | `creating` \| `running` \| `paused` \| `stopped` \| `error` |
| `agent_type` | VARCHAR(50) | NO | AI agent type (default: `claude-code`) |
| `system_prompt` | TEXT | YES | Optional custom system prompt for the agent |
| `last_activity_at` | TIMESTAMPTZ | NO | Timestamp of last user interaction |
| `metadata` | JSONB | YES | Flexible metadata (container stats, config overrides) |
| `created_at` | TIMESTAMPTZ | NO | Session creation timestamp |

### 4.3 Messages Table (Audit Log)

| Column | Type | Nullable | Description |
|--------|------|----------|-------------|
| `id` | UUID | NO | Primary key |
| `session_id` | UUID (FK) | NO | References `sessions.id` |
| `direction` | ENUM | NO | `inbound` (user→agent) \| `outbound` (agent→user) |
| `content_type` | ENUM | NO | `text` \| `file` \| `command` \| `system` |
| `content` | TEXT | NO | Message content or file reference path |
| `telegram_msg_id` | BIGINT | YES | Telegram message ID for reference |
| `processing_ms` | INT | YES | Time taken to process this message |
| `created_at` | TIMESTAMPTZ | NO | Message timestamp |

### 4.4 Indexes

- `users`: UNIQUE on `telegram_id`
- `sessions`: INDEX on `(user_id, status)` for active session lookups
- `sessions`: INDEX on `last_activity_at` for idle container cleanup
- `messages`: INDEX on `(session_id, created_at)` for conversation history

---

## 5. Internal API Specification

All endpoints are internal (`api-server`), called by `telegram-bot`. Not exposed publicly.

### 5.1 Authentication

Internal service-to-service auth via shared secret in `X-Service-Token` header, validated by FastAPI dependency injection. User identity passed as `telegram_id` in request body.

### 5.2 Endpoints

#### User Management

| Method | Endpoint | Description |
|--------|----------|-------------|
| `POST` | `/api/v1/users/register` | Register new user as `guest` (pending approval) |
| `GET` | `/api/v1/users/:telegramId` | Get user profile, approval status, and active session info |
| `POST` | `/api/v1/users/:telegramId/approve` | Admin: approve user (`guest` → `user`, `is_approved = true`) |
| `POST` | `/api/v1/users/:telegramId/reject` | Admin: reject user with optional reason |
| `POST` | `/api/v1/users/:telegramId/revoke` | Admin: revoke access (`user` → `guest`, `is_approved = false`) |
| `GET` | `/api/v1/users?status=pending` | Admin: list users filtered by approval status |
| `PUT` | `/api/v1/users/:telegramId/apikey` | Store encrypted API key (approved users only) |
| `DELETE` | `/api/v1/users/:telegramId/apikey` | Remove stored API key |

#### Session Management

| Method | Endpoint | Description |
|--------|----------|-------------|
| `POST` | `/api/v1/sessions` | Create new session → provisions Docker container |
| `GET` | `/api/v1/sessions/:id` | Get session details (status, container info, uptime) |
| `POST` | `/api/v1/sessions/:id/stop` | Stop container (preserves volume) |
| `POST` | `/api/v1/sessions/:id/restart` | Restart container |
| `DELETE` | `/api/v1/sessions/:id` | Destroy session and remove container + volume |

#### Container Interaction

| Method | Endpoint | Description |
|--------|----------|-------------|
| `POST` | `/api/v1/sessions/:id/exec` | Execute command in container, stream output via SSE |
| `POST` | `/api/v1/sessions/:id/message` | Send message to AI agent, stream response via SSE |
| `POST` | `/api/v1/sessions/:id/upload` | Upload file to container workspace (multipart) |
| `GET` | `/api/v1/sessions/:id/download/:path` | Download file from container workspace |

---

## 6. Telegram Bot Commands & UX

### 6.1 Command Reference

| Command | Arguments | Behavior |
|---------|-----------|----------|
| `/start` | _(none)_ | Welcome message + registration. New users are registered as `guest` (pending approval). Notifies admins for approval |
| `/myid` | _(none)_ | Show your Telegram numeric ID (available to all users, even unregistered — used for admin setup) |
| `/new` | `[agent] [prompt]` | Create new session (approved users only). Optional agent (default: `claude-code`) and system prompt |
| `/stop` | _(none)_ | Stop active container (preserves workspace). Confirm with inline keyboard |
| `/restart` | _(none)_ | Restart active container |
| `/status` | _(none)_ | Show container status: uptime, CPU, RAM, disk usage |
| `/destroy` | _(none)_ | Destroy session + workspace permanently. Double confirm |
| `/setkey` | `<api_key>` | Store API key for AI provider (message auto-deleted for security) |
| `/setprovider` | `<preset>` | Switch provider preset: `anthropic` (default), `openrouter`, `custom` |
| `/setbaseurl` | `<url>` | Set custom API base URL (for OpenRouter, proxies, etc.) |
| `/removekey` | _(none)_ | Remove stored API key and provider config |
| `/provider` | _(none)_ | Show current provider configuration (type + base URL, never shows key) |
| `/shell` | `<command>` | Execute raw shell command in container (bypass AI agent) |
| `/download` | `<filepath>` | Download file from container workspace |
| `/history` | `[n]` | Show last N messages in session (default: 10) |
| `/help` | _(none)_ | Show all available commands with descriptions |
| `/approve` | `<telegram_id>` | _(Admin only)_ Approve a pending user, promotes `guest` → `user` |
| `/reject` | `<telegram_id>` | _(Admin only)_ Reject a pending user with optional reason |
| `/revoke` | `<telegram_id>` | _(Admin only)_ Revoke an approved user's access back to `guest` |
| `/users` | `[pending\|approved\|all]` | _(Admin only)_ List users filtered by approval status (default: `pending`) |

### 6.2 Default Message Behavior

Any message that is NOT a command (no `/` prefix) is treated as a prompt to the AI agent inside the container. This is the primary interaction mode:

1. User types natural language message
2. Bot shows "typing..." indicator
3. Message is forwarded to Claude Code inside the container
4. Response is streamed back and sent as one or more Telegram messages

### 6.3 User Registration & Approval Flow

New users go through a gated approval process before they can access AI agent containers:

1. User sends `/start` to the bot
2. Bot registers user as `guest` with `is_approved = false`
3. Bot replies: "✅ Registered! Your request has been sent to an admin for approval. You'll be notified once approved."
4. Bot sends notification to all `ADMIN_TELEGRAM_IDS` with the user's info and inline keyboard:
   ```
   🆕 New user registration:
   Name: {display_name}
   Username: @{username}
   Telegram ID: {telegram_id}
   
   [✅ Approve]  [❌ Reject]
   ```
5. Admin taps `[✅ Approve]` → user promoted to `user` role, `is_approved = true`
6. Bot notifies the user: "🎉 You've been approved! Use /setkey to configure your API key, then /new to start a session."

**If a pending user tries to use restricted commands:**
Bot replies: "⏳ Your account is pending admin approval. You'll be notified once approved."

**Admin can also approve/reject via commands:**
- `/approve 123456789` — approve by Telegram ID
- `/reject 123456789 reason: not authorized` — reject with optional reason
- `/revoke 123456789` — revoke a previously approved user

### 6.4 Message Formatting Rules

| Scenario | Formatting |
|----------|-----------|
| Code blocks in response | Wrap in Telegram MarkdownV2 code blocks (` ```language\ncode``` `) |
| Response > 4096 chars | Split into multiple messages at logical boundaries (newlines, code block ends) |
| File output from agent | Auto-send as Telegram document with caption showing filepath |
| Error from container | Prefix with ⚠️ emoji, show error message in monospace |
| Container not running | Show inline keyboard: `[Start Container]` `[Create New]` |
| Long-running command | Send initial "Processing..." then edit message with final result |

### 6.5 Inline Keyboards

Used for confirmations and quick actions:

- New user registered (sent to admin): `[✅ Approve]` `[❌ Reject]`
- Pending user tries restricted action: `[Check Status]`
- Container destroyed: `[Create New Session]`
- Container stopped: `[Restart]` `[Destroy]`
- First message, no session: `[Create Container]` `[Set API Key First]` `[Configure Provider]`
- Error state: `[Restart Container]` `[View Logs]` `[Destroy & Recreate]`

---

## 7. Docker Container Specification

### 7.1 Base Image

A custom Docker image built for the agent runtime:

```dockerfile
# Dockerfile.agent
FROM ubuntu:22.04

# System deps
RUN apt-get update && apt-get install -y \
    curl git build-essential python3 python3-pip \
    nodejs npm wget unzip jq \
    && rm -rf /var/lib/apt/lists/*

# Install uv for the bridge process
RUN curl -LsSf https://astral.sh/uv/install.sh | sh
ENV PATH="/root/.local/bin:$PATH"

# Install Claude Code (native binary)
RUN curl -fsSL https://claude.ai/install.sh | bash
ENV PATH="/root/.claude/bin:$PATH"

# Agent bridge (Python WebSocket server)
COPY agent-bridge/ /opt/agent-bridge/
RUN cd /opt/agent-bridge && uv sync --frozen

# Workspace
WORKDIR /workspace
VOLUME ["/workspace"]

# The bridge listens for commands from middleware
EXPOSE 9100
CMD ["uv", "run", "--directory", "/opt/agent-bridge", "python", "-m", "agent_bridge.main", "--port", "9100"]
```

### 7.2 Agent Bridge Process

A lightweight Python asyncio process inside each container that acts as the communication interface between the middleware and the AI agent. It:

- Listens on port 9100 for WebSocket connections from `container-manager` (using `websockets` library)
- Accepts JSON-RPC messages: `execute_prompt`, `run_shell`, `upload_file`, `download_file`, `health_check`
- Spawns Claude Code CLI with `--dangerously-skip-permissions` flag via `asyncio.create_subprocess_exec` (required for headless/non-TTY execution inside containers)
- Manages the AI agent's conversation context in-memory
- Reports container resource usage on `health_check`

> ⚠️ **Security note:** The `--dangerously-skip-permissions` flag allows Claude Code to execute file writes, shell commands, and tool calls without interactive user approval. This is acceptable because each container is fully isolated (see §8.2), runs as non-root, has resource limits enforced, and the user has explicitly opted in by creating a session.

### 7.3 Resource Limits (Per Container)

| Resource | Default Limit | Configurable Range |
|----------|--------------|-------------------|
| CPU | 1 core | 0.5 – 2 cores |
| Memory | 2 GB | 1 – 4 GB |
| Disk (workspace volume) | 5 GB | 1 – 20 GB |
| Network | Egress allowed (for npm/pip) | Allowlist configurable |
| PIDs | 256 | 128 – 512 |
| Idle timeout | 30 minutes | 10 – 120 minutes |

### 7.4 Container Lifecycle

| State | Trigger | Action |
|-------|---------|--------|
| `creating` | User sends `/new` | Pull image (if needed), create container, attach volume, start |
| `running` | Container started | Agent bridge ready, accepting messages |
| `paused` | Idle timeout reached | `docker pause` — freezes processes, preserves memory state |
| `running` | New message while paused | `docker unpause` — resumes instantly (< 1s) |
| `stopped` | User sends `/stop` | `docker stop` — graceful shutdown, volume preserved |
| `running` | User sends `/restart` | `docker restart` — fresh agent process, workspace intact |
| `removed` | User sends `/destroy` | `docker rm -v` — container and volume deleted permanently |

---

## 8. Security Specification

### 8.1 API Key & Provider Configuration

Users can configure their AI provider credentials and optionally use a custom base URL (e.g., OpenRouter, local proxies, or other API-compatible providers).

**Supported commands:**

| Command | Example | Description |
|---------|---------|-------------|
| `/setkey` | `/setkey sk-ant-abc123` | Store API key (message auto-deleted) |
| `/setprovider` | `/setprovider openrouter` | Switch to a preset provider profile |
| `/setbaseurl` | `/setbaseurl https://openrouter.ai/api` | Set custom API base URL |
| `/removekey` | `/removekey` | Remove stored credentials |
| `/provider` | `/provider` | Show current provider configuration |

**Provider presets:**

| Preset | Base URL | Env Vars Injected |
|--------|----------|------------------|
| `anthropic` (default) | _(none, uses Claude Code default)_ | `ANTHROPIC_API_KEY=<user_key>` |
| `openrouter` | `https://openrouter.ai/api` | `ANTHROPIC_BASE_URL`, `ANTHROPIC_AUTH_TOKEN=<user_key>`, `ANTHROPIC_API_KEY=""` |
| `custom` | User-provided URL | `ANTHROPIC_BASE_URL=<url>`, `ANTHROPIC_AUTH_TOKEN=<user_key>`, `ANTHROPIC_API_KEY=""` |

**Key handling flow:**

1. User sends `/setkey sk-ant-...` in Telegram DM
2. Bot immediately deletes the user's message (Telegram `deleteMessage` API)
3. API key encrypted with AES-256-GCM using the `cryptography` library and a server-side master key (env variable)
4. Encrypted key + IV stored in `users.api_key_encrypted` and `users.api_key_iv`
5. Provider config (base URL, provider type) stored in `users.provider_config` (JSONB)
6. Decrypted only in-memory when injecting into container as environment variables
7. Environment variables injected into the container at start based on provider type:

```bash
# Default (Anthropic direct)
ANTHROPIC_API_KEY=<decrypted_key>

# OpenRouter or custom base URL
ANTHROPIC_BASE_URL=<base_url>
ANTHROPIC_AUTH_TOKEN=<decrypted_key>
ANTHROPIC_API_KEY=""  # Must be explicitly empty
```

### 8.2 Container Isolation

- Each container runs in its own Docker network namespace
- No inter-container communication (no shared Docker network)
- Containers run as non-root user (uid 1000) inside the container
- Read-only root filesystem with writable `/workspace` and `/tmp`
- Seccomp and AppArmor profiles applied (Docker defaults)
- No privileged mode, no host PID/network/IPC namespaces

### 8.3 Middleware Security

- Telegram bot webhook verified via `secret_token` header
- Internal APIs not exposed to internet (Docker internal network only)
- Rate limiting: 20 messages/minute per user, 5 container operations/minute
- All database queries parameterized (SQLAlchemy ORM prevents SQL injection)
- Docker socket access restricted to `container-manager` service only

### 8.4 User Authorization

| Action | Guest (pending) | User (approved) | Admin |
|--------|----------------|-----------------|-------|
| `/start`, `/help`, `/myid` | ✅ | ✅ | ✅ |
| `/status` (own approval) | ✅ (see own status) | ✅ | ✅ |
| Send messages to agent | ❌ | ✅ | ✅ |
| `/new`, `/stop`, `/restart` | ❌ | ✅ (own) | ✅ (any) |
| `/shell` (raw commands) | ❌ | ✅ (own) | ✅ (any) |
| `/destroy` | ❌ | ✅ (own) | ✅ (any) |
| `/setkey`, `/setprovider` | ❌ | ✅ | ✅ |
| View all users/sessions | ❌ | ❌ | ✅ |
| `/approve`, `/reject`, `/users` | ❌ | ❌ | ✅ |
| Change user roles | ❌ | ❌ | ✅ |

---

## 9. Deployment Specification

### 9.1 Docker Compose Structure

```yaml
# docker-compose.yml
version: "3.8"
services:
  caddy:
    image: caddy:2
    ports: ["80:80", "443:443"]
    volumes: ["./Caddyfile:/etc/caddy/Caddyfile"]

  telegram-bot:
    build: ./services/telegram-bot
    environment:
      - BOT_TOKEN=${TELEGRAM_BOT_TOKEN}
      - API_SERVER_URL=http://api-server:8000
      - WEBHOOK_SECRET=${WEBHOOK_SECRET}
    depends_on: [api-server]

  api-server:
    build: ./services/api-server
    environment:
      - DATABASE_URL=postgresql+asyncpg://...
      - REDIS_URL=redis://redis:6379
      - ENCRYPTION_KEY=${ENCRYPTION_KEY}
      - CONTAINER_MANAGER_URL=http://container-manager:8001
    depends_on: [postgres, redis]

  container-manager:
    build: ./services/container-manager
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock
      - agent-workspaces:/workspaces
    environment:
      - AGENT_IMAGE=chatops/claude-agent:latest
      - MAX_CONTAINERS=${MAX_CONTAINERS:-20}

  postgres:
    image: postgres:16-alpine
    volumes: ["pgdata:/var/lib/postgresql/data"]

  redis:
    image: redis:7-alpine
    volumes: ["redisdata:/data"]

volumes:
  pgdata:
  redisdata:
  agent-workspaces:
```

### 9.2 Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `TELEGRAM_BOT_TOKEN` | Yes | Bot token from @BotFather |
| `WEBHOOK_SECRET` | Yes | Random string for webhook verification |
| `WEBHOOK_DOMAIN` | Yes | Public domain for Telegram webhook (e.g., `chatops.example.com`) |
| `ENCRYPTION_KEY` | Yes | 32-byte hex string for AES-256-GCM API key encryption |
| `DATABASE_URL` | Yes | PostgreSQL connection string (`postgresql+asyncpg://user:pass@host/db`) |
| `REDIS_URL` | Yes | Redis connection string |
| `ADMIN_TELEGRAM_IDS` | Yes | Comma-separated Telegram IDs with admin role |
| `AGENT_IMAGE` | No | Docker image for agent containers (default: `chatops/claude-agent:latest`) |
| `MAX_CONTAINERS` | No | Maximum total containers across all users (default: `20`) |
| `IDLE_TIMEOUT_MINUTES` | No | Minutes before idle container is paused (default: `30`) |
| `DEFAULT_ANTHROPIC_KEY` | No | Shared API key for users without their own key |
| `DEFAULT_PROVIDER` | No | Default provider for new users: `anthropic`, `openrouter`, `custom` (default: `anthropic`) |
| `DEFAULT_BASE_URL` | No | Default base URL when provider is `openrouter` or `custom` |

### 9.3 Admin Bootstrap

The first admin must be configured manually before the bot can approve other users:

1. Deploy the bot and start it
2. Send `/myid` to the bot in Telegram — it replies with your numeric Telegram ID (e.g., `123456789`)
3. Add your ID to the `.env` file: `ADMIN_TELEGRAM_IDS=123456789`
4. Restart the stack: `docker compose up -d`
5. Send `/start` to the bot — you'll be registered as `admin` with `is_approved = true` automatically

Multiple admins can be added as comma-separated IDs: `ADMIN_TELEGRAM_IDS=123456789,987654321`

> **Note:** `/myid` is the only command (besides `/start` and `/help`) that works without registration or approval. It exists solely to solve the bootstrap problem of needing your Telegram ID before you can configure the system.

### 9.4 Minimum Server Requirements

| Resource | Minimum (10 users) | Recommended (20 users) |
|----------|-------------------|----------------------|
| CPU | 4 vCPU | 8 vCPU |
| RAM | 8 GB | 16 GB |
| Storage | 50 GB SSD | 100 GB SSD |
| OS | Ubuntu 22.04 LTS | Ubuntu 22.04 LTS |
| Docker | 24.0+ | 24.0+ with BuildKit |
| Network | Static IP + domain | Static IP + domain |

---

## 10. Monitoring & Observability

### 10.1 Health Checks

| Service | Endpoint | Interval | Failure Action |
|---------|----------|----------|---------------|
| `telegram-bot` | `GET /health` | 15s | Restart via Docker |
| `api-server` | `GET /health` | 15s | Restart via Docker |
| `container-manager` | `GET /health` | 15s | Restart via Docker |
| Agent containers | WebSocket ping | 30s | Mark session as error, notify user |
| PostgreSQL | `pg_isready` | 10s | Alert admin via Telegram |
| Redis | `redis-cli ping` | 10s | Alert admin via Telegram |

### 10.2 Key Metrics

- `message_latency_ms`: Time from Telegram webhook to response sent (histogram)
- `container_count`: Active, paused, and total containers (gauge)
- `container_start_time_ms`: Time to provision new container (histogram)
- `messages_per_user`: Message count per user per hour (counter)
- `api_key_usage`: API calls per user per day (counter for cost tracking)
- `error_rate`: Failed message deliveries or container errors (counter)

### 10.3 Admin Notifications (via Telegram)

The bot sends alerts to `ADMIN_TELEGRAM_IDS` for:

- **New user registration (with inline approve/reject buttons)**
- Container provisioning failures
- Server resource usage > 80% (CPU/RAM/disk)
- Service health check failures
- Unusual activity (rate limit breaches)

---

## 11. Error Handling Strategy

| Error Scenario | User-Facing Response | System Action |
|---------------|---------------------|--------------|
| Container not found for user | "No active session. Use /new to create one." + inline keyboard | Log warning, check if container was cleaned up |
| Container in error state | "⚠️ Container error. [Restart] [Destroy & Recreate]" | Capture container logs, store in messages table |
| Docker daemon unreachable | "⚠️ System maintenance. Please try again in a few minutes." | Alert admin, retry connection with backoff |
| API key missing/invalid | "Please set your API key with /setkey. Use /setprovider if using OpenRouter or a custom provider." | Log failed auth attempt |
| Rate limit exceeded | "Slowing down! Please wait {N} seconds." | Redis sorted set tracks per-user timestamps |
| Message too long for Telegram | Auto-split into multiple messages | Split at code block boundaries, max 4096 chars |
| File too large (> 50MB) | "File exceeds Telegram's 50MB limit. Use /shell to manage files directly." | Log file size for analytics |
| Container OOM killed | "⚠️ Container ran out of memory and was restarted." | Auto-restart container, log OOM event |
| Max containers reached | "Server at capacity. Stop another session first or try again later." | Log capacity event, alert admin |

---

## 12. Project Structure

```
chatops-ai-bridge/
├── docker-compose.yml
├── Caddyfile
├── .env.example
├── pyproject.toml                   # Workspace root (uv workspace)
├── uv.lock                          # Single lockfile for all packages
├── packages/
│   └── shared/
│       ├── pyproject.toml           # uv package: chatops-shared
│       └── src/
│           └── chatops_shared/
│               ├── __init__.py
│               ├── schemas/
│               │   ├── message.py   # MessageEvent, MessageDirection, ContentType (Pydantic)
│               │   ├── session.py   # SessionStatus, SessionDTO
│               │   └── user.py      # UserRole, UserDTO
│               ├── encryption.py    # AES-256-GCM encrypt/decrypt helpers
│               ├── message_splitter.py  # Split long messages for Telegram
│               └── config.py        # Shared pydantic-settings base
├── services/
│   ├── telegram-bot/
│   │   ├── pyproject.toml           # uv package: chatops-telegram-bot
│   │   ├── Dockerfile
│   │   └── src/
│   │       └── telegram_bot/
│   │           ├── __init__.py
│   │           ├── main.py          # Bot entrypoint, webhook setup
│   │           ├── config.py        # Bot-specific settings (pydantic-settings)
│   │           ├── commands/        # Command handlers (/start, /new, /shell...)
│   │           │   ├── __init__.py
│   │           │   ├── start.py
│   │           │   ├── session.py   # /new, /stop, /restart, /destroy
│   │           │   ├── shell.py     # /shell raw command execution
│   │           │   ├── files.py     # /download, file upload handler
│   │           │   └── admin.py     # /setkey, /removekey, /status
│   │           ├── handlers/        # Message handler, callback query handler
│   │           │   ├── __init__.py
│   │           │   ├── message.py   # Default message → AI agent routing
│   │           │   └── callback.py  # Inline keyboard callbacks
│   │           ├── keyboards.py     # Inline keyboard builders
│   │           ├── formatters.py    # Response formatters (code blocks, errors)
│   │           └── api_client.py    # httpx async client for api-server
│   ├── api-server/
│   │   ├── pyproject.toml           # uv package: chatops-api-server
│   │   ├── Dockerfile
│   │   └── src/
│   │       └── api_server/
│   │           ├── __init__.py
│   │           ├── main.py          # FastAPI app factory, lifespan events
│   │           ├── config.py        # API server settings (pydantic-settings)
│   │           ├── dependencies.py  # FastAPI deps (db session, auth, redis)
│   │           ├── routers/
│   │           │   ├── __init__.py
│   │           │   ├── users.py     # /api/v1/users/* endpoints
│   │           │   ├── sessions.py  # /api/v1/sessions/* endpoints
│   │           │   └── health.py    # /health endpoint
│   │           ├── services/
│   │           │   ├── __init__.py
│   │           │   ├── user_service.py
│   │           │   ├── session_service.py
│   │           │   └── message_service.py
│   │           ├── db/
│   │           │   ├── __init__.py
│   │           │   ├── engine.py    # AsyncEngine + async_sessionmaker setup
│   │           │   ├── models.py    # SQLAlchemy 2.0 mapped classes
│   │           │   └── migrations/  # Alembic migrations
│   │           │       ├── env.py
│   │           │       └── versions/
│   │           └── middleware/
│   │               ├── __init__.py
│   │               ├── auth.py      # Service token verification
│   │               ├── rate_limit.py # Redis-backed rate limiter
│   │               └── error_handler.py
│   └── container-manager/
│       ├── pyproject.toml           # uv package: chatops-container-manager
│       ├── Dockerfile
│       └── src/
│           └── container_manager/
│               ├── __init__.py
│               ├── main.py          # FastAPI app, lifespan (init aiodocker)
│               ├── config.py        # Container manager settings
│               ├── docker_client.py # aiodocker wrapper (create, exec, attach)
│               ├── health.py        # Container health monitor (async tasks)
│               ├── cleanup.py       # Idle container pausing/cleanup job
│               └── routers.py       # Internal API routes
├── images/
│   └── claude-agent/
│       ├── Dockerfile               # Agent container image
│       └── agent-bridge/            # Python bridge process
│           ├── pyproject.toml
│           └── src/
│               └── agent_bridge/
│                   ├── __init__.py
│                   ├── main.py      # WebSocket server entrypoint
│                   ├── handlers.py  # execute_prompt, run_shell, upload, download
│                   └── claude.py    # Claude Code CLI wrapper (--dangerously-skip-permissions)
└── scripts/
    ├── setup.sh                     # First-time server setup
    ├── backup.sh                    # Database + volume backup
    └── seed_admin.py                # Create initial admin user
```

---

## 13. Development Milestones

| Week | Milestone | Deliverables |
|------|-----------|-------------|
| 1 | Foundation | uv workspace scaffold, Docker Compose, SQLAlchemy models + Alembic migrations, Telegram bot with `/start` and `/help` |
| 2 | Container Core | aiodocker integration, container create/start/stop/destroy, agent base image build, health checks |
| 3 | Agent Bridge | WebSocket bridge inside container, Claude Code CLI wrapper, message send/receive flow end-to-end |
| 4 | Full Bot UX | All Telegram commands, inline keyboards, file upload/download, message splitting, error handling |
| 5 | Security + Polish | API key encryption, rate limiting, container isolation hardening, admin notifications, deployment docs |
| 6 | Testing + Launch | pytest integration tests, load testing (20 concurrent users), staging deploy, production launch |

---

## 14. MVP Acceptance Criteria

The MVP is considered complete when all of the following are verified:

1. A new user can `/start` the bot and register as a pending guest
2. Admin receives an inline-keyboard notification and can approve/reject the user
3. Approved user can store their API key via `/setkey` and the message is auto-deleted
4. Pending users are blocked from all container and agent commands with a clear message
5. User can create a new container session via `/new` and receive confirmation within 15 seconds
4. User can send natural language prompts and receive Claude Code responses in chat
5. User can execute raw shell commands via `/shell` and see stdout/stderr output
6. User can upload a file to the container workspace via Telegram file attachment
7. User can download a file from the container via `/download`
8. Long responses (> 4096 chars) are correctly split across multiple Telegram messages
9. Container auto-pauses after 30 minutes idle and resumes on next message
10. User can `/stop`, `/restart`, and `/destroy` their container
11. Admin receives Telegram notifications for errors and new registrations
12. System handles 20 concurrent active containers without degradation
13. All API keys are encrypted at rest and never logged in plaintext
14. Container cannot access other containers or the host filesystem
