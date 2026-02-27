# Teledocker

A Telegram bot that provisions per-user Docker containers running AI Agents/Claude Code.
Each user gets an isolated sandbox with persistent workspace, streamed responses,
and file upload/download — all managed through Telegram commands.

## Architecture

```
Telegram --> Caddy --> telegram-bot --> api-server --> container-manager
                                           |                 |
                                        postgres       Docker Engine
                                         redis         (user containers)
```

Five services orchestrated with Docker Compose:

| Service | Role |
|---------|------|
| **telegram-bot** | Telegram interface (commands, message routing, SSE rendering) |
| **api-server** | Central REST API, user/session management, database, encryption |
| **container-manager** | Docker container lifecycle, health monitoring, idle cleanup |
| **postgres** | User, session, and message persistence |
| **redis** | Session cache, pub/sub notifications, rate limiting |

Plus **Caddy** as a reverse proxy (production webhook mode) and the
**claude-agent** Docker image that runs inside each user container.


## Prerequisites

- Python 3.12+
- [uv](https://docs.astral.sh/uv/) package manager
- Docker 24+ with Compose v2
- A Telegram bot token from [@BotFather](https://t.me/BotFather)


## Quick Start (Docker)

The fastest way to get running. Everything runs inside containers.

### 1. Clone and configure

```bash
git clone <repo-url> teledocker && cd teledocker
cp .env.example .env
```

Edit `.env` and fill in the required values:

```bash
# Get from @BotFather
TELEGRAM_BOT_TOKEN=your_bot_token

# Generate secrets
ENCRYPTION_KEY=$(openssl rand -hex 32)
SERVICE_TOKEN=$(openssl rand -hex 32)

# Your Telegram user ID (send /myid to any ID bot)
ADMIN_TELEGRAM_IDS=123456789

# Database password
POSTGRES_PASSWORD=a_strong_password_here
```

### 2. Run setup

```bash
chmod +x scripts/setup.sh
./scripts/setup.sh
```

This script will:
- Build all Docker images (including the claude-agent base image)
- Pull postgres, redis, and caddy
- Start infrastructure services and wait for readiness
- Run database migrations (Alembic)
- Seed the admin user from `ADMIN_TELEGRAM_IDS`
- Start all services

### 3. Verify

```bash
docker compose ps
```

All services should show `healthy`. Send `/start` to your bot on Telegram —
you should be auto-approved as admin.

### Rebuilding after code changes

```bash
# Rebuild a single service
docker compose build api-server
docker compose up -d api-server

# Rebuild everything
docker compose build
docker compose up -d

# Rebuild the claude-agent base image (build-only profile)
docker compose build claude-agent
```


## Development Mode

For local development with hot-reload, debuggers, and fast iteration.

### 1. Install dependencies

The project uses a `uv` workspace. One command installs everything:

```bash
uv sync
```

This installs all five workspace members and the shared `chatops-shared` package
in development mode:

```
packages/shared          -> chatops-shared (DTOs, encryption, message splitting)
services/api-server      -> api-server
services/container-manager -> container-manager
services/telegram-bot    -> telegram-bot
images/claude-agent/agent-bridge -> agent-bridge
```

### 2. Start infrastructure

You still need postgres and redis. Run them via Docker:

```bash
docker compose up -d postgres redis
```

Wait for health checks to pass:

```bash
docker compose ps  # Both should show "healthy"
```

### 3. Configure environment

Create a `.env` file in the project root (see `.env.example`). For development,
set `BOT_MODE=polling` — this avoids needing a public domain for webhooks.

Export the variables to your shell:

```bash
export $(grep -v '^#' .env | xargs)
```

Or use a tool like [direnv](https://direnv.net/) to load `.env` automatically.

### 4. Run database migrations

```bash
cd services/api-server
uv run alembic upgrade head
cd ../..
```

### 5. Start services

Open three terminal windows (or use tmux/screen):

```bash
# Terminal 1: API server
uv run uvicorn api_server.main:app --host 0.0.0.0 --port 8000 --reload

# Terminal 2: Container manager
uv run uvicorn container_manager.main:app --host 0.0.0.0 --port 8001 --reload

# Terminal 3: Telegram bot
uv run python -m telegram_bot.main
```

The `--reload` flag on uvicorn gives hot-reload on file changes.

### 6. Seed admin user

```bash
uv run python scripts/seed_admin.py
```

### Running tests

```bash
# All tests
uv run pytest

# Per-service
uv run pytest services/api-server/tests/
uv run pytest services/telegram-bot/tests/
uv run pytest services/container-manager/tests/

# Single test file
uv run pytest services/api-server/tests/test_sse_proxy.py -v
```

### Linting and formatting

```bash
# Check for issues
uv run ruff check .

# Auto-fix
uv run ruff check --fix .

# Format
uv run ruff format .
```

Ruff is configured in the root `pyproject.toml`: line length 100, Python 3.12
target, with rules E, F, I, UP, B, SIM enabled.


## Production Deployment

### 1. Server requirements

- A Linux server (Ubuntu 22.04+ recommended)
- Docker 24+ with Compose v2
- A domain name pointed to your server (for HTTPS and Telegram webhooks)
- Ports 80 and 443 open

### 2. Clone and configure

```bash
git clone <repo-url> /opt/teledocker && cd /opt/teledocker
cp .env.example .env
```

Edit `.env` for production:

```bash
# Telegram
TELEGRAM_BOT_TOKEN=your_bot_token
BOT_MODE=webhook
WEBHOOK_SECRET=$(openssl rand -hex 32)
WEBHOOK_DOMAIN=chatops.yourdomain.com

# Security — generate unique values for production
ENCRYPTION_KEY=$(openssl rand -hex 32)
SERVICE_TOKEN=$(openssl rand -hex 32)

# Admin
ADMIN_TELEGRAM_IDS=123456789

# Database — use a strong password
POSTGRES_PASSWORD=$(openssl rand -base64 24)

# Optional tuning
MAX_CONTAINERS=20
IDLE_TIMEOUT_MINUTES=30
LOG_LEVEL=WARNING
```

Key differences from development:
- `BOT_MODE=webhook` — Telegram pushes updates to your server instead of polling
- `WEBHOOK_DOMAIN` — your public domain (Caddy auto-provisions HTTPS via Let's Encrypt)
- `WEBHOOK_SECRET` — verifies that webhook requests actually come from Telegram

### 3. DNS setup

Create an A record pointing `chatops.yourdomain.com` to your server's IP.
Caddy will automatically obtain a TLS certificate from Let's Encrypt on first
request.

### 4. Deploy

```bash
chmod +x scripts/setup.sh
./scripts/setup.sh
```

### 5. Verify

```bash
# Check all services are healthy
docker compose ps

# Check logs
docker compose logs -f telegram-bot
docker compose logs -f api-server

# Verify webhook is accessible
curl -s https://chatops.yourdomain.com/webhook
# Should return 404 for GET (expected — Telegram sends POST)
```

### 6. Set the Telegram webhook

If the bot doesn't auto-register the webhook on startup, set it manually:

```bash
curl -s "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/setWebhook" \
  -d "url=https://${WEBHOOK_DOMAIN}/webhook" \
  -d "secret_token=${WEBHOOK_SECRET}"
```

### Caddy configuration

The included `Caddyfile` only exposes the `/webhook` path. All internal APIs
(api-server on :8000, container-manager on :8001) are never reachable from the
internet.

```
chatops.yourdomain.com {
    handle /webhook {
        reverse_proxy telegram-bot:8080
    }
    respond 404
}
```

### Backups

```bash
# Database backup (compressed SQL dump)
./scripts/backup.sh

# Custom backup directory
./scripts/backup.sh /mnt/backups
```

Backups are saved as `postgres_YYYYMMDD_HHMMSS.sql.gz`.

### Updating

```bash
cd /opt/teledocker
git pull
docker compose build
docker compose up -d
```

Alembic migrations run automatically on api-server startup, so schema changes
are applied during each deploy.


## Environment Variables Reference

### Required

| Variable | Description | Example |
|----------|-------------|---------|
| `TELEGRAM_BOT_TOKEN` | Bot token from @BotFather | `123456:ABC-DEF` |
| `ENCRYPTION_KEY` | 32-byte hex for AES-256-GCM encryption | `openssl rand -hex 32` |
| `SERVICE_TOKEN` | Shared secret for inter-service auth | `openssl rand -hex 32` |
| `ADMIN_TELEGRAM_IDS` | Comma-separated admin Telegram IDs | `123456789,987654321` |
| `POSTGRES_PASSWORD` | PostgreSQL password | any strong password |

### Required for production

| Variable | Description | Example |
|----------|-------------|---------|
| `BOT_MODE` | `polling` (dev) or `webhook` (prod) | `webhook` |
| `WEBHOOK_SECRET` | Telegram webhook verification secret | `openssl rand -hex 32` |
| `WEBHOOK_DOMAIN` | Public domain for webhook endpoint | `chatops.example.com` |

### Optional

| Variable | Default | Description |
|----------|---------|-------------|
| `AGENT_IMAGE` | `chatops/claude-agent:latest` | Docker image for user containers |
| `MAX_CONTAINERS` | `20` | Maximum total containers across all users |
| `IDLE_TIMEOUT_MINUTES` | `30` | Minutes before idle containers are paused |
| `DESTROY_TIMEOUT_HOURS` | `24` | Hours before stopped containers are destroyed |
| `DEFAULT_PROVIDER` | `anthropic` | Default AI provider (`anthropic`, `openrouter`, `custom`) |
| `DEFAULT_ANTHROPIC_KEY` | — | Shared fallback API key for users without their own |
| `DEFAULT_BASE_URL` | — | Base URL for openrouter/custom providers |
| `LOG_LEVEL` | `INFO` | Logging verbosity (`DEBUG`, `INFO`, `WARNING`, `ERROR`) |


## Telegram Commands

### User commands

| Command | Description |
|---------|-------------|
| `/start` | Register and get welcome message |
| `/help` | Show available commands |
| `/myid` | Show your Telegram user ID |
| `/new [name]` | Create a new Claude Code session |
| `/stop` | Pause your active session |
| `/restart` | Resume a paused session |
| `/destroy` | Delete your session and container |
| `/status` | Show session and container status |
| `/shell <cmd>` | Execute a raw shell command in your container |
| `/upload` | Upload a file (reply to a document with this command) |
| `/download <path>` | Download a file from your container |
| `/setkey` | Store your API key (message is auto-deleted) |
| `/setprovider <name>` | Set your AI provider |
| `/setbaseurl <url>` | Set base URL for custom provider |
| `/removekey` | Delete your stored API key |

### Admin commands

| Command | Description |
|---------|-------------|
| `/approve <user_id>` | Approve a pending user |
| `/reject <user_id>` | Reject a user request |
| `/revoke <user_id>` | Revoke an approved user's access |
| `/users` | List all users and their status |
| `/containers` | List all running containers |
| `/provider <user_id>` | Set provider config for a user |

Any text message sent without a command is forwarded to the user's active Claude
Code session as a prompt.


## Network Architecture

```
                   Internet
                      |
               [ports 80, 443]
                      |
                    Caddy (/webhook only)
                      |
              +----- internal network -----+
              |       |        |           |
         telegram  api-server  postgres   redis
           -bot        |
                       |
              container-manager
                   |       |
           internal net  agent-net
                           |
                    user containers
                    (claude-agent)
```

- **internal**: All core services. Not exposed to user containers.
- **agent-net**: Only container-manager and user containers. Isolates user
  workloads from the rest of the stack.


## Project Structure

```
teledocker/
|-- docker-compose.yml          # Service orchestration
|-- Caddyfile                   # Reverse proxy config
|-- pyproject.toml              # uv workspace root + ruff/pytest config
|-- .env.example                # Environment variable template
|-- scripts/
|   |-- setup.sh                # First-time setup
|   |-- backup.sh               # Database backup
|   `-- seed_admin.py           # Bootstrap admin users
|-- packages/shared/            # Shared library (DTOs, encryption, utils)
|-- services/
|   |-- telegram-bot/           # Telegram bot (python-telegram-bot v21)
|   |-- api-server/             # REST API (FastAPI + SQLAlchemy)
|   `-- container-manager/      # Docker lifecycle (FastAPI + aiodocker)
`-- images/
    `-- claude-agent/           # Base image for user containers
        `-- agent-bridge/       # WebSocket JSON-RPC server (port 9100)
```


## Troubleshooting

### Services fail to start

```bash
# Check logs for a specific service
docker compose logs api-server

# Common issue: postgres not ready yet
docker compose restart api-server
```

### Bot doesn't respond

1. Verify `TELEGRAM_BOT_TOKEN` in `.env`
2. Check `BOT_MODE` matches your setup (polling for dev, webhook for prod)
3. Check telegram-bot logs: `docker compose logs telegram-bot`

### Webhook not working

1. Verify DNS resolves: `dig chatops.yourdomain.com`
2. Check Caddy logs: `docker compose logs caddy`
3. Verify webhook is set: `curl https://api.telegram.org/bot<TOKEN>/getWebhookInfo`
4. Ensure ports 80 and 443 are open in your firewall

### User containers won't start

1. Check container-manager has Docker socket access
2. Verify the claude-agent image is built: `docker images | grep claude-agent`
3. If missing, build it: `docker compose build claude-agent`
4. Check container-manager logs: `docker compose logs container-manager`

### Database issues

```bash
# Connect to the database
docker compose exec postgres psql -U chatops -d chatops

# Re-run migrations
docker compose run --rm api-server uv run alembic upgrade head
```
