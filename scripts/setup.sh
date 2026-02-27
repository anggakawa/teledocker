#!/usr/bin/env bash
# First-time server setup for ChatOps AI Bridge.
#
# Run this once on a fresh server after cloning the repo.
# Requires: Docker 24+, Docker Compose v2.

set -euo pipefail

echo "ChatOps AI Bridge — Setup"
echo "========================="

# Check dependencies.
if ! command -v docker &>/dev/null; then
    echo "ERROR: Docker is not installed."
    exit 1
fi

if ! docker compose version &>/dev/null; then
    echo "ERROR: Docker Compose v2 is not available."
    exit 1
fi

# Copy example env if .env doesn't exist yet.
if [ ! -f .env ]; then
    cp .env.example .env
    echo "Created .env from .env.example — fill in your values before continuing."
    echo "Edit .env now, then re-run this script."
    exit 0
fi

echo "Building Docker images..."
docker compose build

echo "Pulling external images..."
docker compose pull postgres redis caddy

echo "Starting infrastructure services (postgres, redis)..."
docker compose up -d postgres redis

echo "Waiting for PostgreSQL to be ready..."
until docker compose exec postgres pg_isready -U chatops -d chatops &>/dev/null; do
    sleep 1
done
echo "PostgreSQL ready."

echo "Running database migrations..."
docker compose run --rm api-server uv run alembic upgrade head

echo "Seeding initial admin user..."
docker compose run --rm api-server uv run python /workspace/scripts/seed_admin.py

echo "Starting all services..."
docker compose up -d

echo ""
echo "Setup complete!"
echo ""
echo "Next steps:"
echo "  1. Send /myid to your bot to get your Telegram ID"
echo "  2. Add your ID to ADMIN_TELEGRAM_IDS in .env"
echo "  3. Restart: docker compose up -d"
echo "  4. Send /start to the bot — you'll be auto-approved as admin"
