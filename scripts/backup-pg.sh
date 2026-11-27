#!/usr/bin/env bash
# Canvasphere 4C8G / 70GiB ESSD 自动化 Postgres 每日备份脚本（默认保留最近 7 天）
# 推荐 crontab（服务器 admin 用户）：
#   15 3 * * * /opt/canvasphere/scripts/backup-pg.sh >> /opt/canvasphere/backups/backup.log 2>&1
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/canvasphere}"
BACKUP_DIR="${BACKUP_DIR:-${APP_DIR}/backups}"
RETENTION_DAYS="${RETENTION_DAYS:-7}"
TIMESTAMP="$(date -u +%Y%m%dT%H%M%SZ)"

mkdir -p "${BACKUP_DIR}"
cd "${APP_DIR}"

if [ -f .env ]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi

PG_USER="${POSTGRES_USER:-canvas}"
PG_DB="${POSTGRES_DB:-canvas}"
OUT_FILE="${BACKUP_DIR}/canvasphere_${TIMESTAMP}.sql.gz"

echo "[$(date -u +%FT%TZ)] Starting pg_dump for db=${PG_DB}..."
docker compose exec -T db pg_dump -U "${PG_USER}" -d "${PG_DB}" --no-owner --no-acl | gzip -9 > "${OUT_FILE}"
ls -lh "${OUT_FILE}"

echo "[$(date -u +%FT%TZ)] Pruning backups older than ${RETENTION_DAYS} days..."
find "${BACKUP_DIR}" -type f -name 'canvasphere_*.sql.gz' -mtime +"${RETENTION_DAYS}" -delete
echo "[$(date -u +%FT%TZ)] Backup complete."
