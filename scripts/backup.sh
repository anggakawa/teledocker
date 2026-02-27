#!/usr/bin/env bash
# Backup PostgreSQL database and agent workspace volumes.
#
# Usage: ./scripts/backup.sh [backup_dir]
# Default backup directory: ./backups/

set -euo pipefail

BACKUP_DIR="${1:-./backups}"
TIMESTAMP=$(date +%Y%m%d_%H%M%S)

mkdir -p "$BACKUP_DIR"

echo "Backing up PostgreSQL..."
docker compose exec -T postgres pg_dump -U chatops chatops \
    | gzip > "$BACKUP_DIR/postgres_${TIMESTAMP}.sql.gz"
echo "Database backup: $BACKUP_DIR/postgres_${TIMESTAMP}.sql.gz"

echo "Backup complete at $TIMESTAMP"
