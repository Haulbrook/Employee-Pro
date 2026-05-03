#!/usr/bin/env bash
# SENTINEL — layer1-foundation/harden.sh
# OS hardening: UFW firewall, disable unused services, SSH hardening, fail2ban.
# Idempotent — safe to re-run.

set -euo pipefail

LOG_TAG="harden"
log() { logger -t "sentinel-${LOG_TAG}" "$*" 2>/dev/null || true; printf '[harden] %s\n' "$*"; }

require_root() {
    if [[ $EUID -ne 0 ]]; then
        echo "harden.sh must run as root" >&2
        exit 1
    fi
}

# Skip on macOS — UFW + fail2ban are Linux-only and macOS has its own firewall model.
if [[ "$(uname -s)" != "Linux" ]]; then
    log "Non-Linux OS detected ($(uname -s)) — hardening skipped."
    exit 0
fi

require_root

# ---------- UFW firewall -----------------------------------------------------
configure_ufw() {
    log "Configuring UFW firewall..."
    if ! command -v ufw >/dev/null 2>&1; then
        log "UFW not installed — skipping (install with: apt-get install ufw)"
        return
    fi

    ufw --force reset >/dev/null
    ufw default deny incoming
    ufw default allow outgoing

    # Allow SSH only from RFC1918 by default; widen via SENTINEL_SSH_FROM env var.
    local ssh_src="${SENTINEL_SSH_FROM:-10.0.0.0/8 172.16.0.0/12 192.168.0.0/16}"
    for net in $ssh_src; do
        ufw allow from "$net" to any port 22 proto tcp comment 'SENTINEL SSH'
    done

    # Dashboard is bound to 127.0.0.1 only (CT-17 fix), no UFW rule required.
    # Everything else stays denied.

    ufw --force enable
    log "UFW enabled"
}

# ---------- disable unneeded services ----------------------------------------
disable_services() {
    log "Disabling unneeded services..."
    local svc
    for svc in cups bluetooth avahi-daemon ModemManager; do
        if systemctl list-unit-files | grep -q "^${svc}\\.service"; then
            systemctl disable --now "${svc}.service" 2>/dev/null || true
            log "  disabled $svc"
        fi
    done
}

# ---------- SSH hardening ----------------------------------------------------
harden_ssh() {
    log "Hardening SSH configuration..."
    local cfg=/etc/ssh/sshd_config.d/50-sentinel.conf
    if [[ ! -d /etc/ssh/sshd_config.d ]]; then
        log "  sshd_config.d not present — skipping SSH hardening"
        return
    fi
    cat > "$cfg" <<'EOF'
# SENTINEL — managed by harden.sh
PermitRootLogin no
PasswordAuthentication no
ChallengeResponseAuthentication no
KbdInteractiveAuthentication no
PubkeyAuthentication yes
X11Forwarding no
MaxAuthTries 3
LoginGraceTime 30
ClientAliveInterval 300
ClientAliveCountMax 2
Protocol 2
EOF
    chmod 0644 "$cfg"
    if sshd -t 2>/dev/null; then
        systemctl reload ssh 2>/dev/null || systemctl reload sshd 2>/dev/null || true
        log "  SSH config reloaded"
    else
        log "  sshd config test FAILED — leaving previous config"
        rm -f "$cfg"
    fi
}

# ---------- fail2ban ---------------------------------------------------------
configure_fail2ban() {
    log "Configuring fail2ban..."
    if ! command -v fail2ban-client >/dev/null 2>&1; then
        log "  fail2ban not installed — skipping"
        return
    fi
    cat > /etc/fail2ban/jail.d/sentinel.conf <<'EOF'
[DEFAULT]
bantime  = 1h
findtime = 10m
maxretry = 5

[sshd]
enabled = true
EOF
    systemctl enable fail2ban 2>/dev/null || true
    systemctl restart fail2ban 2>/dev/null || true
    log "  fail2ban enabled"
}

# ---------- kernel + sysctl --------------------------------------------------
apply_sysctl() {
    log "Applying sysctl hardening..."
    cat > /etc/sysctl.d/99-sentinel.conf <<'EOF'
# SENTINEL — minimal network hardening
net.ipv4.conf.all.rp_filter = 1
net.ipv4.conf.default.rp_filter = 1
net.ipv4.conf.all.accept_redirects = 0
net.ipv4.conf.default.accept_redirects = 0
net.ipv4.conf.all.send_redirects = 0
net.ipv4.conf.all.accept_source_route = 0
net.ipv4.tcp_syncookies = 1
kernel.dmesg_restrict = 1
EOF
    sysctl --system >/dev/null 2>&1 || true
}

# ---------- log dir ----------------------------------------------------------
ensure_log_dir() {
    install -d -m 0750 -o sentinel -g sentinel /var/log/sentinel 2>/dev/null || \
        install -d -m 0750 /var/log/sentinel
}

# ---------- main -------------------------------------------------------------
main() {
    log "Starting Layer 1 hardening"
    configure_ufw
    disable_services
    harden_ssh
    configure_fail2ban
    apply_sysctl
    ensure_log_dir
    log "Hardening complete"
}

main "$@"
