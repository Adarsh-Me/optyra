#!/usr/bin/env bash
# Nightly Postgres backup (report 01prd §15: pg_dump | gzip -> object storage).
# Suggested cron (on the box, as a user with docker access):
#   15 4 * * *  /opt/optyra/deploy/backup.sh /opt/optyra/backups >> /var/log/optyra-backup.log 2>&1
# Optional off-site: install rclone, configure a Backblaze B2 remote (10 GB free tier),
# then set OPTYRA_RCLONE_REMOTE=b2:optyra-backups in the environment.
set -euo pipefail

BACKUP_DIR="${1:-./backups}"
RETENTION_DAYS="${OPTYRA_BACKUP_RETENTION_DAYS:-14}"
RCLONE_REMOTE="${OPTYRA_RCLONE_REMOTE:-}"
COMPOSE_DIR="$(cd "$(dirname "$0")" && pwd)"
STAMP="$(date +%Y%m%d-%H%M%S)"
OUT="$BACKUP_DIR/optyra-$STAMP.sql.gz"

mkdir -p "$BACKUP_DIR"
trap '[ -f "$OUT.tmp" ] && rm -f "$OUT.tmp"' EXIT

echo "[$(date -Is)] dumping to $OUT"
docker compose --env-file "$COMPOSE_DIR/../.env" -f "$COMPOSE_DIR/docker-compose.yml" exec -T postgres \
  pg_dump -U "${POSTGRES_USER:-optyra}" "${POSTGRES_DB:-optyra}" \
  | gzip > "$OUT.tmp"
mv "$OUT.tmp" "$OUT"
echo "[$(date -Is)] wrote $(du -h "$OUT" | cut -f1)"

# Restore drill (run monthly per runbook.md):
#   gunzip -c "$OUT" | docker compose --env-file "$COMPOSE_DIR/../.env" \
#     -f "$COMPOSE_DIR/docker-compose.yml" exec -T postgres \
#     psql -U "${POSTGRES_USER:-optyra}" -d "${POSTGRES_DB:-optyra}"

if [ -n "$RCLONE_REMOTE" ]; then
  echo "[$(date -Is)] syncing to $RCLONE_REMOTE"
  rclone copy "$OUT" "$RCLONE_REMOTE" --transfers 1
fi

find "$BACKUP_DIR" -name 'optyra-*.sql.gz' -mtime +"$RETENTION_DAYS" -delete
echo "[$(date -Is)] pruned backups older than $RETENTION_DAYS days"
