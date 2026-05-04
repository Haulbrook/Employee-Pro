#!/usr/bin/env bash
# F.1 — manual VM bootstrap test (run on a clean Ubuntu 22.04+ VM)
#
# Procedure:
#   1. Provision a clean Ubuntu 22.04 VM (Multipass, a fresh DO droplet, etc.)
#      Minimum: 4 GB RAM, 10 GB free disk, 1 vCPU, internet egress.
#   2. SSH in as a sudo-capable user.
#   3. Clone SENTINEL into the home dir:
#        git clone https://github.com/Haulbrook/sentinel.git
#        cd sentinel
#   4. Run this script. It walks through the bootstrap end-to-end.

set -euo pipefail

# Pre-flight only first
sudo ./bootstrap.sh --check

# Full bootstrap (will prompt for credentials interactively)
sudo ./bootstrap.sh

# Verifications
echo "--- systemd units ---"
systemctl is-active sentinel-agent
systemctl is-active sentinel-watchdog
systemctl is-active sentinel-dashboard
systemctl is-active sentinel-health.timer

echo "--- dashboard reachable on loopback ---"
curl -fsS http://127.0.0.1:8501/api/status >/dev/null

echo "--- credential file permissions ---"
stat -c '%a %U:%G' /opt/sentinel/.env  # expect: 600 sentinel:sentinel

echo "--- lock file present ---"
test -f /var/sentinel/.installed && echo "lock present"

echo "--- F.1 PASSED on clean VM ---"
