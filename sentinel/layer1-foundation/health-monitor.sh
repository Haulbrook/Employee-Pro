#!/usr/bin/env bash
# SENTINEL — layer1-foundation/health-monitor.sh
# Disk / memory / network health checks. Writes to data/metrics.json.
# Thresholds match the spec (README, watchdog monitoring table):
#   disk    > 85% = warning
#   memory  > 90% = warning
#   network = ping 8.8.8.8 + DNS resolve

set -euo pipefail

SENTINEL_HOME="${SENTINEL_HOME:-/opt/sentinel}"
METRICS_FILE="${SENTINEL_HOME}/data/metrics.json"
INTERVAL="${SENTINEL_HEALTH_INTERVAL:-60}"
DISK_WARN_PCT=85
MEM_WARN_PCT=90

ONCE=0
[[ "${1:-}" == "--once" ]] && ONCE=1

mkdir -p "$(dirname "$METRICS_FILE")"

iso_now() { date -u +%FT%TZ; }

# Returns disk usage % (integer) for the partition holding $SENTINEL_HOME.
disk_usage_pct() {
    df -P "$SENTINEL_HOME" | awk 'NR==2 { gsub("%","",$5); print $5 }'
}

# Returns memory usage % (integer). Uses /proc/meminfo (Linux); falls back to vm_stat on macOS.
memory_usage_pct() {
    if [[ -r /proc/meminfo ]]; then
        awk '
            /MemTotal/   { total = $2 }
            /MemAvailable/ { avail = $2 }
            END { if (total > 0) printf("%d", (total - avail) * 100 / total) }
        ' /proc/meminfo
    elif command -v vm_stat >/dev/null 2>&1; then
        # macOS
        local page_size pages_free pages_active pages_inactive pages_speculative pages_wired
        page_size="$(vm_stat | awk '/page size/ {print $8}')"
        : "${page_size:=4096}"
        pages_free="$(vm_stat | awk '/Pages free/ {gsub("\\.","",$3); print $3}')"
        pages_active="$(vm_stat | awk '/Pages active/ {gsub("\\.","",$3); print $3}')"
        pages_inactive="$(vm_stat | awk '/Pages inactive/ {gsub("\\.","",$3); print $3}')"
        pages_speculative="$(vm_stat | awk '/Pages speculative/ {gsub("\\.","",$3); print $3}')"
        pages_wired="$(vm_stat | awk '/Pages wired/ {gsub("\\.","",$4); print $4}')"
        local used=$(( (pages_active + pages_wired) * page_size ))
        local total=$(( (pages_active + pages_wired + pages_inactive + pages_free + pages_speculative) * page_size ))
        if (( total > 0 )); then
            echo $(( used * 100 / total ))
        else
            echo 0
        fi
    else
        echo 0
    fi
}

# Returns disk free in bytes (for disk-janitor decision making).
disk_free_bytes() {
    df -P "$SENTINEL_HOME" | awk 'NR==2 {print $4 * 1024}'
}

ping_ok() {
    if ping -c 1 -W 2 8.8.8.8 >/dev/null 2>&1; then echo true; else echo false; fi
}

dns_ok() {
    if getent hosts google.com >/dev/null 2>&1; then echo true
    elif command -v host >/dev/null 2>&1 && host -W 2 google.com >/dev/null 2>&1; then echo true
    else echo false
    fi
}

uptime_seconds() {
    if [[ -r /proc/uptime ]]; then
        awk '{print int($1)}' /proc/uptime
    elif command -v sysctl >/dev/null 2>&1; then
        local boot
        boot="$(sysctl -n kern.boottime 2>/dev/null | awk -F'[ ,}]' '{print $4}')"
        if [[ -n "$boot" ]]; then
            echo $(( $(date +%s) - boot ))
        else
            echo 0
        fi
    else
        echo 0
    fi
}

load_average() {
    if [[ -r /proc/loadavg ]]; then
        awk '{print $1}' /proc/loadavg
    elif command -v uptime >/dev/null 2>&1; then
        uptime | awk -F'load average[s]*: ' '{print $2}' | awk -F',' '{print $1}' | tr -d ' '
    else
        echo 0
    fi
}

write_metrics() {
    local disk_pct mem_pct net_ping net_dns disk_free uptime load
    disk_pct="$(disk_usage_pct)"
    mem_pct="$(memory_usage_pct)"
    disk_free="$(disk_free_bytes)"
    net_ping="$(ping_ok)"
    net_dns="$(dns_ok)"
    uptime="$(uptime_seconds)"
    load="$(load_average)"

    local disk_status="ok" mem_status="ok" net_status="ok"
    (( disk_pct > DISK_WARN_PCT )) && disk_status="warning"
    (( mem_pct  > MEM_WARN_PCT  )) && mem_status="warning"
    [[ "$net_ping" == "false" || "$net_dns" == "false" ]] && net_status="warning"

    local overall="ok"
    [[ "$disk_status" == "warning" || "$mem_status" == "warning" || "$net_status" == "warning" ]] && overall="warning"

    cat > "${METRICS_FILE}.tmp" <<EOF
{
  "timestamp": "$(iso_now)",
  "overall": "${overall}",
  "uptime_seconds": ${uptime},
  "load_average_1m": ${load},
  "disk": {
    "usage_pct": ${disk_pct},
    "free_bytes": ${disk_free},
    "warn_threshold_pct": ${DISK_WARN_PCT},
    "status": "${disk_status}"
  },
  "memory": {
    "usage_pct": ${mem_pct},
    "warn_threshold_pct": ${MEM_WARN_PCT},
    "status": "${mem_status}"
  },
  "network": {
    "ping_8_8_8_8": ${net_ping},
    "dns_resolve_google_com": ${net_dns},
    "status": "${net_status}"
  }
}
EOF
    mv "${METRICS_FILE}.tmp" "$METRICS_FILE"
    [[ "$overall" == "warning" ]] && return 1 || return 0
}

if (( ONCE == 1 )); then
    write_metrics
    exit $?
fi

# Loop mode (used by sentinel-health.timer wrapper or direct invocation).
while true; do
    write_metrics || true
    sleep "$INTERVAL"
done
