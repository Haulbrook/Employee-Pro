#!/usr/bin/env python3
"""SENTINEL — watchdog.py (Phase C.2).

The immune system. Monitors every SENTINEL service + the host's network,
disk, memory, and API keys. Restarts services with exponential backoff
(5s → 10s → 20s → 40s → 60s) and quarantines after 5 consecutive failures.

Gate-2 FIX 2 (CT-11): the watchdog itself is monitored by systemd via
``WatchdogSec=30``. We call ``sd_notify("WATCHDOG=1")`` every cycle so
systemd will kill+restart us if we hang. If sd_notify is unavailable we
log a warning but continue (e.g. when run interactively).
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent))
sys.path.insert(0, str(_HERE))

from utils.common import (  # noqa: E402
    METRICS_FILE,
    STATE_FILE,
    DATA_DIR,
    iso_now,
    read_json,
    write_json,
    load_env_file,
    get_logger,
)

LOG = get_logger("watchdog")

CYCLE_SECONDS = 10
WATCHDOG_NOTIFY_SECONDS = 5

# Restart strategy from the README (Layer 4 spec).
BACKOFF_SCHEDULE = [5, 10, 20, 40, 60]
QUARANTINE_AFTER = len(BACKOFF_SCHEDULE)  # 5 fails

DISK_WARN_PCT = 85
MEM_WARN_PCT  = 90


# ---------- sd_notify (Gate-2 FIX 2) ----------------------------------------

def _sd_notify(message: str) -> bool:
    """Send a state change to systemd notify socket. Returns True if sent."""
    sock_path = os.environ.get("NOTIFY_SOCKET")
    if not sock_path:
        return False
    try:
        import socket
        if sock_path.startswith("@"):
            sock_path = "\0" + sock_path[1:]
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        try:
            sock.connect(sock_path)
            sock.sendall(message.encode("utf-8"))
        finally:
            sock.close()
        return True
    except Exception as exc:
        LOG.debug("sd_notify failed: %s", exc)
        return False


# ---------- target abstraction ----------------------------------------------

@dataclass
class Target:
    name: str
    check: Callable[[], bool]
    repair: Callable[[], None]
    fails: int = 0
    quarantined: bool = False
    last_action: str = ""
    history: list[dict] = field(default_factory=list)


def _systemd_active(unit: str) -> bool:
    if not shutil.which("systemctl"):
        return True  # not on systemd host — assume managed manually
    try:
        out = subprocess.run(
            ["systemctl", "is-active", unit],
            capture_output=True, text=True, timeout=5,
        )
        return out.stdout.strip() == "active"
    except Exception as exc:
        LOG.warning("is-active %s failed: %s", unit, exc)
        return False


def _systemd_restart(unit: str) -> None:
    if not shutil.which("systemctl"):
        LOG.warning("systemctl not present; cannot restart %s", unit)
        return
    LOG.info("restarting %s", unit)
    subprocess.run(["systemctl", "restart", unit], timeout=30, check=False)


def _quarantine(target: Target) -> None:
    target.quarantined = True
    LOG.error("quarantining target=%s after %d consecutive failures",
              target.name, target.fails)
    try:
        from quarantine import quarantine as do_quarantine  # sibling import
        do_quarantine(component=target.name, reason="watchdog-max-restarts")
    except Exception as exc:
        LOG.error("quarantine module call failed: %s", exc)


# ---------- target builders -------------------------------------------------

def _agent_target() -> Target:
    return Target(
        name="sentinel-agent",
        check=lambda: _systemd_active("sentinel-agent.service"),
        repair=lambda: _systemd_restart("sentinel-agent.service"),
    )


def _dashboard_target() -> Target:
    return Target(
        name="sentinel-dashboard",
        check=lambda: _systemd_active("sentinel-dashboard.service"),
        repair=lambda: _systemd_restart("sentinel-dashboard.service"),
    )


def _network_target() -> Target:
    def _check() -> bool:
        m = read_json(METRICS_FILE, default={})
        return (m.get("network", {}).get("status") == "ok")

    def _repair() -> None:
        # Network repair is delegated to network-sentinel.py, which handles the
        # connectivity state machine. Watchdog just makes sure it's running.
        if shutil.which("systemctl"):
            subprocess.run(["systemctl", "restart", "sentinel-agent.service"],
                           timeout=30, check=False)

    return Target(name="network", check=_check, repair=_repair)


def _disk_target() -> Target:
    def _check() -> bool:
        m = read_json(METRICS_FILE, default={})
        return int(m.get("disk", {}).get("usage_pct", 0)) <= DISK_WARN_PCT

    def _repair() -> None:
        janitor = _HERE / "disk-janitor.sh"
        if janitor.exists():
            LOG.info("triggering disk-janitor")
            subprocess.run(["bash", str(janitor)], timeout=300, check=False)
        else:
            LOG.warning("disk-janitor.sh not found at %s", janitor)

    return Target(name="disk", check=_check, repair=_repair)


def _memory_target() -> Target:
    def _check() -> bool:
        m = read_json(METRICS_FILE, default={})
        return int(m.get("memory", {}).get("usage_pct", 0)) <= MEM_WARN_PCT

    def _repair() -> None:
        # Spec: "Kill lowest-priority process". We only kill processes we own
        # (sentinel-* services) to avoid collateral damage on shared machines.
        if not shutil.which("systemctl"):
            return
        for unit in ("sentinel-dashboard.service", "sentinel-agent.service"):
            LOG.warning("memory pressure: restarting %s", unit)
            subprocess.run(["systemctl", "restart", unit], timeout=30, check=False)
            break  # restart one at a time

    return Target(name="memory", check=_check, repair=_repair)


def _api_key_target() -> Target:
    def _check() -> bool:
        env = load_env_file()
        from utils.validator import validate_api_key
        if env.get("ANTHROPIC_API_KEY"):
            return validate_api_key("anthropic", env["ANTHROPIC_API_KEY"], timeout=5)
        if env.get("OPENAI_API_KEY"):
            return validate_api_key("openai", env["OPENAI_API_KEY"], timeout=5)
        # No keys configured — not a watchdog failure, just skip.
        return True

    def _repair() -> None:
        try:
            from credential_refresh import refresh
            refresh()
        except Exception as exc:
            LOG.error("credential refresh failed: %s", exc)

    return Target(name="api-keys", check=_check, repair=_repair)


# ---------- main loop -------------------------------------------------------

def _build_targets() -> list[Target]:
    return [
        _agent_target(),
        _dashboard_target(),
        _network_target(),
        _disk_target(),
        _memory_target(),
        _api_key_target(),
    ]


def _persist_status(targets: list[Target]) -> None:
    state = read_json(STATE_FILE, default={})
    state["watchdog"] = {
        "evaluated_at": iso_now(),
        "targets": [
            {
                "name": t.name,
                "fails": t.fails,
                "quarantined": t.quarantined,
                "last_action": t.last_action,
            }
            for t in targets
        ],
    }
    state["quarantined_components"] = [t.name for t in targets if t.quarantined]
    write_json(STATE_FILE, state)


def run_loop() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    targets = _build_targets()
    _sd_notify("READY=1\nSTATUS=watchdog cycling")

    running = True
    def _stop(signum, _frame):  # noqa: ARG001
        nonlocal running
        LOG.info("stopping (signal %s)", signum)
        running = False
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    last_notify = 0.0

    while running:
        for t in targets:
            if t.quarantined:
                continue
            healthy = False
            try:
                healthy = bool(t.check())
            except Exception as exc:
                LOG.error("check raised for %s: %s", t.name, exc)
            if healthy:
                if t.fails:
                    LOG.info("recovered: %s (after %d fails)", t.name, t.fails)
                t.fails = 0
                t.last_action = "ok"
                continue

            t.fails += 1
            if t.fails >= QUARANTINE_AFTER:
                _quarantine(t)
                t.last_action = "quarantine"
                continue

            backoff = BACKOFF_SCHEDULE[min(t.fails - 1, len(BACKOFF_SCHEDULE) - 1)]
            LOG.warning("unhealthy: %s fails=%d backoff=%ds → repair",
                        t.name, t.fails, backoff)
            try:
                t.repair()
                t.last_action = f"repair (backoff {backoff}s)"
            except Exception as exc:
                LOG.error("repair raised for %s: %s", t.name, exc)
                t.last_action = f"repair-error: {exc}"
            time.sleep(backoff)

        _persist_status(targets)

        now = time.time()
        if now - last_notify >= WATCHDOG_NOTIFY_SECONDS:
            _sd_notify("WATCHDOG=1")
            last_notify = now

        time.sleep(CYCLE_SECONDS)

    _sd_notify("STOPPING=1")
    LOG.info("watchdog stopped")


def main(argv: list[str] | None = None) -> int:
    if argv and "--once" in argv:
        # one-shot for tests / cron
        targets = _build_targets()
        for t in targets:
            try:
                ok = bool(t.check())
            except Exception as exc:
                ok = False
                LOG.error("check %s: %s", t.name, exc)
            print(f"{t.name}: {'ok' if ok else 'unhealthy'}")
            if not ok:
                try:
                    t.repair()
                except Exception as exc:
                    LOG.error("repair %s: %s", t.name, exc)
        _persist_status(targets)
        return 0
    run_loop()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
