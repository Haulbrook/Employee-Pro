#!/usr/bin/env bash
# SENTINEL — layer1-foundation/scheduler.sh
# Cross-platform scheduler installer. On Linux (systemd) this is a thin
# wrapper that confirms the SENTINEL systemd timers are installed; on
# macOS it provides equivalent launchd plist instructions.

set -euo pipefail

case "$(uname -s)" in
    Linux)
        if ! command -v systemctl >/dev/null 2>&1; then
            echo "[scheduler] systemd not detected — install systemd or run manually." >&2
            exit 1
        fi
        echo "[scheduler] Verifying SENTINEL systemd units..."
        for unit in sentinel-agent.service sentinel-watchdog.service \
                    sentinel-dashboard.service sentinel-health.timer; do
            if systemctl list-unit-files | grep -q "^${unit}"; then
                echo "  ok: $unit"
            else
                echo "  missing: $unit (re-run bootstrap.sh --upgrade)" >&2
            fi
        done
        ;;
    Darwin)
        echo "[scheduler] macOS detected. Use launchd to schedule SENTINEL services."
        echo "  See docs/SETUP.md (macOS section) for plist examples."
        ;;
    *)
        echo "[scheduler] Unsupported OS: $(uname -s)" >&2
        exit 1
        ;;
esac
