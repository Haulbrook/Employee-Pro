#!/usr/bin/env bash
# SENTINEL v1.0 — bootstrap.sh
# Single entry point that turns a blank Ubuntu/macOS machine into a
# fully operational SENTINEL instance.
#
# Usage:
#   sudo ./bootstrap.sh                 # full install
#   sudo ./bootstrap.sh --reinstall     # wipe + reinstall
#   sudo ./bootstrap.sh --upgrade       # in-place upgrade
#   sudo ./bootstrap.sh --check         # run pre-flight only
#
# Implements Gate-2 remediation:
#   FIX 1  pre-flight checks (OS, internet, disk, RAM, idempotency)
#   FIX 3  credential security (chmod 600, owner = sentinel)

set -euo pipefail

SENTINEL_VERSION="1.0.0"
SENTINEL_HOME="/opt/sentinel"
SENTINEL_USER="sentinel"
SENTINEL_GROUP="sentinel"
SENTINEL_LOG_DIR="/var/log/sentinel"
SENTINEL_LOCK_FILE="/var/sentinel/.installed"
SENTINEL_REPO_DEFAULT="https://github.com/Haulbrook/sentinel.git"
SENTINEL_REPO="${SENTINEL_REPO:-$SENTINEL_REPO_DEFAULT}"

MIN_DISK_GB=10
MIN_RAM_GB=4
MIN_UBUNTU_VERSION="22.04"
MIN_MACOS_VERSION="12"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MODE="install"

# ---------- output helpers ---------------------------------------------------
c_red()    { printf '\033[0;31m%s\033[0m' "$*"; }
c_green()  { printf '\033[0;32m%s\033[0m' "$*"; }
c_yellow() { printf '\033[0;33m%s\033[0m' "$*"; }
c_blue()   { printf '\033[0;34m%s\033[0m' "$*"; }

log_info()  { printf '[%s] %s %s\n' "$(date -u +%FT%TZ)" "$(c_blue '[INFO]')"  "$*"; }
log_warn()  { printf '[%s] %s %s\n' "$(date -u +%FT%TZ)" "$(c_yellow '[WARN]')" "$*" >&2; }
log_error() { printf '[%s] %s %s\n' "$(date -u +%FT%TZ)" "$(c_red '[ERR]')"   "$*" >&2; }
log_ok()    { printf '[%s] %s %s\n' "$(date -u +%FT%TZ)" "$(c_green '[OK]')"   "$*"; }

die() { log_error "$*"; exit 1; }

banner() {
    cat <<'EOF'
   _____  ______ _   _ _______ _____ _   _ ______ _
  / ____||  ____| \ | |__   __|_   _| \ | |  ____| |
 | (___  | |__  |  \| |  | |    | | |  \| | |__  | |
  \___ \ |  __| | . ` |  | |    | | | . ` |  __| | |
  ____) || |____| |\  |  | |   _| |_| |\  | |____| |____
 |_____/ |______|_| \_|  |_|  |_____|_| \_|______|______|

  SENTINEL v1.0 — autonomous workstation bootstrap
EOF
}

# ---------- argument parsing -------------------------------------------------
parse_args() {
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --reinstall) MODE="reinstall" ;;
            --upgrade)   MODE="upgrade" ;;
            --check)     MODE="check" ;;
            -h|--help)
                grep -E '^# (Usage|  )' "$0" | sed 's/^# //'
                exit 0
                ;;
            *) die "Unknown argument: $1" ;;
        esac
        shift
    done
}

# ---------- Gate-2 pre-flight checks ----------------------------------------

# CT-01 fix: detect supported OS
check_os() {
    log_info "Checking operating system..."
    local kernel
    kernel="$(uname -s)"
    case "$kernel" in
        Linux)
            [[ -r /etc/os-release ]] || die "Cannot read /etc/os-release"
            # shellcheck disable=SC1091
            . /etc/os-release
            DETECTED_OS="$ID"
            DETECTED_OS_VERSION="$VERSION_ID"
            if [[ "$ID" != "ubuntu" && "$ID_LIKE" != *ubuntu* && "$ID_LIKE" != *debian* ]]; then
                die "Unsupported Linux distribution: $ID. SENTINEL targets Ubuntu 22.04+."
            fi
            local major
            major="${VERSION_ID%%.*}"
            if (( major < 22 )); then
                die "Ubuntu $VERSION_ID is too old. Minimum is $MIN_UBUNTU_VERSION."
            fi
            log_ok "OS: $PRETTY_NAME"
            ;;
        Darwin)
            DETECTED_OS="macos"
            DETECTED_OS_VERSION="$(sw_vers -productVersion)"
            local major="${DETECTED_OS_VERSION%%.*}"
            if (( major < MIN_MACOS_VERSION )); then
                die "macOS $DETECTED_OS_VERSION is too old. Minimum is $MIN_MACOS_VERSION."
            fi
            log_ok "OS: macOS $DETECTED_OS_VERSION (note: full feature set requires Linux)"
            ;;
        *)
            die "Unsupported kernel: $kernel. SENTINEL runs on Linux (Ubuntu) or macOS."
            ;;
    esac
}

# CT-02 fix: connectivity check
check_internet() {
    log_info "Checking internet connectivity..."
    if ! ping -c 1 -W 3 8.8.8.8 >/dev/null 2>&1; then
        die "Cannot reach 8.8.8.8. SENTINEL needs internet during bootstrap."
    fi
    if ! getent hosts google.com >/dev/null 2>&1 && ! host google.com >/dev/null 2>&1; then
        die "DNS resolution failed for google.com."
    fi
    log_ok "Internet: reachable + DNS working"
}

# CT-03 fix: disk space check (require 10GB+)
check_disk_space() {
    log_info "Checking disk space..."
    local target="${SENTINEL_HOME%/*}"
    [[ -d "$target" ]] || target="/"
    local available_kb
    available_kb="$(df -Pk "$target" | awk 'NR==2 {print $4}')"
    local available_gb=$(( available_kb / 1024 / 1024 ))
    if (( available_gb < MIN_DISK_GB )); then
        die "Insufficient disk space: ${available_gb}GB free, need ${MIN_DISK_GB}GB."
    fi
    log_ok "Disk: ${available_gb}GB free"
}

# Gate-2 addition: RAM check
check_ram() {
    log_info "Checking memory..."
    local ram_gb
    if [[ "$DETECTED_OS" == "macos" ]]; then
        local ram_bytes
        ram_bytes="$(sysctl -n hw.memsize)"
        ram_gb=$(( ram_bytes / 1024 / 1024 / 1024 ))
    else
        local ram_kb
        ram_kb="$(awk '/MemTotal/ {print $2}' /proc/meminfo)"
        ram_gb=$(( ram_kb / 1024 / 1024 ))
    fi
    if (( ram_gb < MIN_RAM_GB )); then
        die "Insufficient RAM: ${ram_gb}GB, need ${MIN_RAM_GB}GB."
    fi
    log_ok "RAM: ${ram_gb}GB"
}

# CT-04 fix: idempotency
check_idempotency() {
    log_info "Checking for existing install..."
    if [[ -f "$SENTINEL_LOCK_FILE" ]]; then
        local existing_version
        existing_version="$(cat "$SENTINEL_LOCK_FILE" 2>/dev/null || echo unknown)"
        case "$MODE" in
            install)
                cat <<EOF >&2
$(c_yellow '[WARN]') Existing install detected (version: $existing_version).
Re-run with one of:
   sudo $0 --upgrade     # in-place upgrade, keeps state and credentials
   sudo $0 --reinstall   # full wipe and reinstall
EOF
                exit 1
                ;;
            upgrade|reinstall|check)
                log_ok "Existing install: $existing_version (mode: $MODE)"
                ;;
        esac
    else
        if [[ "$MODE" == "upgrade" ]]; then
            die "No existing install to upgrade."
        fi
        log_ok "Clean install"
    fi
}

run_preflight() {
    log_info "Running pre-flight checks..."
    check_os
    check_internet
    check_disk_space
    check_ram
    check_idempotency
    log_ok "Pre-flight passed"
}

# ---------- privilege check --------------------------------------------------
require_root() {
    if [[ $EUID -ne 0 ]]; then
        die "bootstrap.sh must run as root (try: sudo $0)"
    fi
}

# ---------- package install --------------------------------------------------
install_packages() {
    log_info "Installing system packages..."
    if [[ "$DETECTED_OS" == "macos" ]]; then
        if ! command -v brew >/dev/null 2>&1; then
            die "Homebrew required on macOS. Install from https://brew.sh and re-run."
        fi
        brew install python@3.11 git jq curl coreutils
    else
        export DEBIAN_FRONTEND=noninteractive
        apt-get update -qq
        apt-get install -y --no-install-recommends \
            python3 python3-pip python3-venv \
            git jq curl ufw fail2ban \
            logrotate systemd ca-certificates
    fi
    log_ok "Packages installed"
}

# ---------- user + directory layout ------------------------------------------
create_sentinel_user() {
    if [[ "$DETECTED_OS" == "macos" ]]; then
        log_info "Skipping user creation on macOS (uses current user)"
        SENTINEL_USER="$(stat -f '%Su' "$SCRIPT_DIR")"
        SENTINEL_GROUP="$(stat -f '%Sg' "$SCRIPT_DIR")"
        return
    fi
    if id "$SENTINEL_USER" >/dev/null 2>&1; then
        log_ok "User $SENTINEL_USER already exists"
    else
        log_info "Creating $SENTINEL_USER user..."
        useradd --system --home "$SENTINEL_HOME" --shell /usr/sbin/nologin "$SENTINEL_USER"
        log_ok "Created user $SENTINEL_USER"
    fi
}

create_directories() {
    log_info "Creating directory layout..."
    install -d -m 0755 "$SENTINEL_HOME"
    install -d -m 0755 "$SENTINEL_HOME"/{config,utils,layer1-foundation,layer2-agent,layer3-autonomy,layer4-selfheal,layer5-dashboard,services,data,docs}
    install -d -m 0750 "$SENTINEL_LOG_DIR"
    install -d -m 0750 "$(dirname "$SENTINEL_LOCK_FILE")"
    install -d -m 0700 "$SENTINEL_HOME/data/outputs"
    install -d -m 0700 "$SENTINEL_HOME/data/quarantine"
    if [[ "$DETECTED_OS" != "macos" ]]; then
        chown -R "$SENTINEL_USER:$SENTINEL_GROUP" "$SENTINEL_HOME" "$SENTINEL_LOG_DIR"
    fi
    log_ok "Directories ready"
}

# ---------- code deploy ------------------------------------------------------
deploy_code() {
    log_info "Deploying SENTINEL code..."
    # Prefer local checkout (running from repo); fall back to git clone.
    if [[ -f "$SCRIPT_DIR/bootstrap.sh" && -d "$SCRIPT_DIR/layer1-foundation" ]]; then
        log_info "Using local checkout: $SCRIPT_DIR"
        rsync -a --delete \
            --exclude='.git' \
            --exclude='data/' \
            --exclude='*.pyc' \
            --exclude='__pycache__/' \
            "$SCRIPT_DIR"/ "$SENTINEL_HOME"/
    else
        log_info "Cloning from $SENTINEL_REPO"
        local tmp
        tmp="$(mktemp -d)"
        git clone --depth=1 "$SENTINEL_REPO" "$tmp"
        rsync -a --exclude='.git' "$tmp"/ "$SENTINEL_HOME"/
        rm -rf "$tmp"
    fi
    if [[ "$DETECTED_OS" != "macos" ]]; then
        chown -R "$SENTINEL_USER:$SENTINEL_GROUP" "$SENTINEL_HOME"
    fi
    find "$SENTINEL_HOME" -name '*.sh' -exec chmod +x {} \;
    log_ok "Code deployed to $SENTINEL_HOME"
}

# ---------- Python deps ------------------------------------------------------
install_python_deps() {
    log_info "Installing Python dependencies..."
    local venv="$SENTINEL_HOME/.venv"
    python3 -m venv "$venv"
    "$venv/bin/pip" install --quiet --upgrade pip
    if [[ -f "$SENTINEL_HOME/requirements.txt" ]]; then
        "$venv/bin/pip" install --quiet -r "$SENTINEL_HOME/requirements.txt"
    else
        "$venv/bin/pip" install --quiet \
            requests pyyaml anthropic systemd-python
    fi
    if [[ "$DETECTED_OS" != "macos" ]]; then
        chown -R "$SENTINEL_USER:$SENTINEL_GROUP" "$venv"
    fi
    log_ok "Python venv ready at $venv"
}

# ---------- credentials (FIX 3) ----------------------------------------------
prompt_credentials() {
    local env_file="$SENTINEL_HOME/.env"
    if [[ -f "$env_file" && "$MODE" == "upgrade" ]]; then
        log_ok "Existing credentials preserved (.env left untouched)"
        return
    fi

    log_info "Collecting credentials (input is hidden)..."
    local template="$SENTINEL_HOME/.env.template"
    if [[ ! -f "$template" ]]; then
        log_warn ".env.template not found, generating minimal one"
        cat > "$template" <<'TPL'
ANTHROPIC_API_KEY=
OPENAI_API_KEY=
GAS_ENDPOINT_URL=
SENTINEL_SECRET=
SENTINEL_INSTANCE_ID=SENTINEL-01
SMTP_HOST=
SMTP_PORT=587
SMTP_USER=
SMTP_PASSWORD=
SMTP_FROM=
SLACK_WEBHOOK_URL=
TPL
    fi

    cp "$template" "$env_file"

    prompt_secret() {
        local key="$1" label="$2" value
        printf '  %s: ' "$label" >&2
        read -r -s value
        printf '\n' >&2
        if [[ -n "$value" ]]; then
            # Escape backslashes and double quotes for safe inclusion
            local escaped
            escaped="${value//\\/\\\\}"
            escaped="${escaped//\"/\\\"}"
            sed -i.bak "s|^${key}=.*|${key}=\"${escaped}\"|" "$env_file"
            rm -f "${env_file}.bak"
        fi
    }

    prompt_secret ANTHROPIC_API_KEY  "Anthropic (Claude) API key (leave blank to skip)"
    prompt_secret OPENAI_API_KEY     "OpenAI API key (leave blank to skip)"
    prompt_secret GAS_ENDPOINT_URL   "Google Apps Script web app URL"
    prompt_secret SENTINEL_SECRET    "SENTINEL shared secret (any random string)"
    prompt_secret SLACK_WEBHOOK_URL  "Slack webhook URL (leave blank to skip)"

    # FIX 3 — credential security
    chmod 600 "$env_file"
    if [[ "$DETECTED_OS" != "macos" ]]; then
        chown "$SENTINEL_USER:$SENTINEL_GROUP" "$env_file"
    fi
    log_ok "Credentials saved to $env_file (mode 600)"
}

# ---------- harden + services ------------------------------------------------
run_hardening() {
    if [[ "$DETECTED_OS" == "macos" ]]; then
        log_info "Skipping OS hardening on macOS (UFW/fail2ban not applicable)"
        return
    fi
    log_info "Running Layer 1 hardening..."
    bash "$SENTINEL_HOME/layer1-foundation/harden.sh"
    log_ok "Hardening complete"
}

register_services() {
    if [[ "$DETECTED_OS" == "macos" ]]; then
        log_info "Skipping systemd registration on macOS (use launchd or run manually)"
        return
    fi
    log_info "Registering systemd services..."
    local svc_src="$SENTINEL_HOME/services"
    cp "$svc_src"/sentinel-*.service /etc/systemd/system/
    cp "$svc_src"/sentinel-*.timer   /etc/systemd/system/ 2>/dev/null || true
    systemctl daemon-reload
    for unit in sentinel-agent sentinel-watchdog sentinel-dashboard; do
        systemctl enable "${unit}.service"
    done
    systemctl enable sentinel-health.timer 2>/dev/null || true
    log_ok "systemd units installed and enabled"
}

start_services() {
    if [[ "$DETECTED_OS" == "macos" ]]; then
        log_info "Skipping service start on macOS"
        return
    fi
    log_info "Starting services..."
    for unit in sentinel-agent sentinel-watchdog sentinel-dashboard; do
        systemctl restart "${unit}.service"
    done
    systemctl start sentinel-health.timer 2>/dev/null || true
    log_ok "Services started"
}

run_health_check() {
    log_info "Running initial health check..."
    bash "$SENTINEL_HOME/layer1-foundation/health-monitor.sh" --once || \
        log_warn "Health check reported issues — see metrics.json"
    log_ok "Health check complete"
}

write_lock_file() {
    install -d -m 0750 "$(dirname "$SENTINEL_LOCK_FILE")"
    printf '%s\n' "$SENTINEL_VERSION" > "$SENTINEL_LOCK_FILE"
    chmod 0640 "$SENTINEL_LOCK_FILE"
}

# ---------- final banner -----------------------------------------------------
print_online_banner() {
    local dash_url="http://127.0.0.1:8501"
    cat <<EOF

$(c_green '╔══════════════════════════════════════════════════════════╗')
$(c_green '║                                                          ║')
$(c_green '║                  ✅  SENTINEL ONLINE                     ║')
$(c_green '║                                                          ║')
$(c_green '╚══════════════════════════════════════════════════════════╝')

  Version:     $SENTINEL_VERSION
  Install:     $SENTINEL_HOME
  User:        $SENTINEL_USER
  Logs:        $SENTINEL_LOG_DIR
  Dashboard:   $dash_url

  Next steps:
    • Open the dashboard from this machine
    • Add tasks to your Google Sheets queue
    • Tail logs:  sudo journalctl -u sentinel-agent -f

EOF
}

# ---------- main -------------------------------------------------------------
main() {
    parse_args "$@"
    banner
    require_root
    run_preflight
    [[ "$MODE" == "check" ]] && { log_ok "Pre-flight only — exiting."; exit 0; }

    if [[ "$MODE" == "reinstall" ]]; then
        log_warn "Reinstall mode: removing existing install"
        rm -rf "$SENTINEL_HOME" "$SENTINEL_LOCK_FILE"
    fi

    install_packages
    create_sentinel_user
    create_directories
    deploy_code
    install_python_deps
    prompt_credentials
    run_hardening
    register_services
    start_services
    run_health_check
    write_lock_file
    print_online_banner
}

main "$@"
