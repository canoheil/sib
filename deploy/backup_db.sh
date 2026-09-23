#!/usr/bin/env bash
set -euo pipefail

# Database Backup Script for Goje MariaDB
BACKUP_DIR="${BACKUP_DIR:-/opt/goje/backups}"
TIMESTAMP=$(date +"%Y%m%d_%H%M%S")
BACKUP_FILE="${BACKUP_DIR}/goje_backup_${TIMESTAMP}.sql.gz"

DB_USER="${DB_USER:-goje}"
DB_PASSWORD="${DB_PASSWORD:-goje_password}"
DB_NAME="${DB_NAME:-goje}"
DB_HOST="${DB_HOST:-localhost}"

mkdir -p "$BACKUP_DIR"

echo "🍅 [1/2] Creating compressed MariaDB dump for database: ${DB_NAME}..."
mysqldump -h "$DB_HOST" -u "$DB_USER" -p"$DB_PASSWORD" \
    --single-transaction \
    --quick \
    --default-character-set=utf8mb4 \
    "$DB_NAME" | gzip > "$BACKUP_FILE"

echo "✅ [2/2] Backup successfully created at: ${BACKUP_FILE} ($(du -h "$BACKUP_FILE" | cut -f1))"

# Retention: Keep last 14 backups, delete older
find "$BACKUP_DIR" -type f -name "goje_backup_*.sql.gz" -mtime +14 -delete || true
echo "🧹 Old backups pruned (retained 14 days)."
