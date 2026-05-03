#!/usr/bin/env bash
# SENTINEL — layer4-selfheal/disk-janitor.sh
# Triggered by watchdog when disk usage > 85%.
# Behaviour:
#   * purge logs older than 7 days under /var/log/sentinel
#   * rotate metrics.json if > 50 MB → metrics-YYYY-MM-DD.json.gz
#   * keep 30 days of metrics archives
#   * vacuum journal logs to 200 MB cap (best-effort)

set -euo pipefail

SENTINEL_HOME="${SENTINEL_HOME:-/opt/sentinel}"
LOG_DIR="${SENTINEL_LOG_DIR:-/var/log/sentinel}"
DATA_DIR="${SENTINEL_HOME}/data"
METRICS_FILE="${DATA_DIR}/metrics.json"
ARCHIVE_DIR="${DATA_DIR}/metrics-archive"

METRICS_ROTATE_BYTES=$((50 * 1024 * 1024))   # 50 MB
ARCHIVE_RETENTION_DAYS=30
LOG_RETENTION_DAYS=7

log() { printf '[disk-janitor] %s\n' "$*"; }

mkdir -p "$ARCHIVE_DIR"

# 1) Purge old logs
if [[ -d "$LOG_DIR" ]]; then
    log "Purging logs older than ${LOG_RETENTION_DAYS} days in $LOG_DIR"
    find "$LOG_DIR" -type f \( -name '*.log.*' -o -name '*.gz' \) \
        -mtime +"${LOG_RETENTION_DAYS}" -print -delete || true
fi

# 2) Rotate metrics.json if oversized
if [[ -f "$METRICS_FILE" ]]; then
    size=$(stat -c '%s' "$METRICS_FILE" 2>/dev/null || stat -f '%z' "$METRICS_FILE")
    if (( size > METRICS_ROTATE_BYTES )); then
        date_tag=$(date -u +%Y-%m-%d)
        archive="${ARCHIVE_DIR}/metrics-${date_tag}.json"
        log "Rotating metrics.json (size=${size} bytes) → ${archive}.gz"
        cp "$METRICS_FILE" "$archive"
        gzip -f "$archive"
        # truncate without disturbing readers
        : > "$METRICS_FILE"
    fi
fi

# 3) Prune old archives
log "Pruning metrics archives older than ${ARCHIVE_RETENTION_DAYS} days"
find "$ARCHIVE_DIR" -type f -name 'metrics-*.json.gz' \
    -mtime +"${ARCHIVE_RETENTION_DAYS}" -print -delete || true

# 4) Vacuum journal (best-effort; only if journalctl is present and we are root)
if command -v journalctl >/dev/null 2>&1 && [[ $EUID -eq 0 ]]; then
    log "Vacuuming systemd journal to 200M"
    journalctl --vacuum-size=200M >/dev/null 2>&1 || true
fi

# 5) Clean Python __pycache__ and tmp under SENTINEL_HOME
find "$SENTINEL_HOME" -type d -name '__pycache__' -exec rm -rf {} + 2>/dev/null || true
find "${SENTINEL_HOME}/data/quarantine" -type f -mtime +30 -print -delete 2>/dev/null || true

log "Disk janitor complete"
