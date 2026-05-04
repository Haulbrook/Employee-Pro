#!/usr/bin/env python3
"""SENTINEL — quarantine.py (Phase C.3).

Isolate a failing component so the rest of SENTINEL keeps running.

Component map: which systemd unit each logical name corresponds to. When a
target name isn't a service (e.g. "disk", "network"), no unit is stopped —
we still record the quarantine context for the dashboard.

Quarantine artefact: data/quarantine/<component>.json with timestamp,
reason, last metrics snapshot, and how to release.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent))

from utils.common import (  # noqa: E402
    QUARANTINE_DIR,
    METRICS_FILE,
    STATE_FILE,
    iso_now,
    read_json,
    write_json,
    get_logger,
)

LOG = get_logger("quarantine")

# Map logical component names → systemd unit (or None for non-service targets).
COMPONENT_UNIT: dict[str, str | None] = {
    "sentinel-agent":     "sentinel-agent.service",
    "sentinel-watchdog":  "sentinel-watchdog.service",
    "sentinel-dashboard": "sentinel-dashboard.service",
    "network": None,
    "disk":    None,
    "memory":  None,
    "api-keys": None,
}


def _systemctl(action: str, unit: str) -> int:
    if not shutil.which("systemctl"):
        LOG.warning("systemctl not present — cannot %s %s", action, unit)
        return 0
    return subprocess.run(["systemctl", action, unit], timeout=30, check=False).returncode


def quarantine(component: str, reason: str = "watchdog-max-restarts") -> Path:
    """Quarantine ``component``. Returns the path to the artefact file."""
    QUARANTINE_DIR.mkdir(parents=True, exist_ok=True)
    artefact = QUARANTINE_DIR / f"{component}.json"

    unit = COMPONENT_UNIT.get(component)
    actions: list[str] = []
    if unit:
        rc = _systemctl("stop", unit)
        actions.append(f"systemctl stop {unit} → rc={rc}")
        # Mask so systemd won't auto-restart on next boot until released.
        mc = _systemctl("mask", unit)
        actions.append(f"systemctl mask {unit} → rc={mc}")

    metrics = read_json(METRICS_FILE, default={})
    payload = {
        "component": component,
        "unit": unit,
        "reason": reason,
        "quarantined_at": iso_now(),
        "actions": actions,
        "metrics_snapshot": metrics,
        "release_command": f"sudo systemctl unmask {unit} && sudo systemctl start {unit}"
                            if unit else
                            "remove this file under data/quarantine/ to release",
    }
    write_json(artefact, payload)

    # Update state.json so the dashboard sees the quarantined component.
    state = read_json(STATE_FILE, default={})
    components = list(set(state.get("quarantined_components", []) + [component]))
    state["quarantined_components"] = sorted(components)
    state["last_quarantine_at"] = payload["quarantined_at"]
    write_json(STATE_FILE, state)

    LOG.error("quarantined %s (reason=%s)", component, reason)
    return artefact


def release(component: str) -> bool:
    """Release a quarantined component. Returns True on success."""
    artefact = QUARANTINE_DIR / f"{component}.json"
    if not artefact.exists():
        LOG.warning("no quarantine artefact for %s", component)
        return False

    unit = COMPONENT_UNIT.get(component)
    if unit:
        _systemctl("unmask", unit)
        _systemctl("start", unit)

    artefact.unlink(missing_ok=True)

    state = read_json(STATE_FILE, default={})
    state["quarantined_components"] = sorted(
        c for c in state.get("quarantined_components", []) if c != component
    )
    state["last_release_at"] = iso_now()
    write_json(STATE_FILE, state)

    LOG.info("released %s", component)
    return True


def list_quarantined() -> list[dict]:
    if not QUARANTINE_DIR.exists():
        return []
    out: list[dict] = []
    for p in sorted(QUARANTINE_DIR.glob("*.json")):
        try:
            with open(p, "r", encoding="utf-8") as fh:
                out.append(json.load(fh))
        except Exception as exc:
            LOG.warning("could not read %s: %s", p, exc)
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="quarantine")
    sub = parser.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("add"); a.add_argument("component"); a.add_argument("--reason", default="manual")
    r = sub.add_parser("release"); r.add_argument("component")
    sub.add_parser("list")
    args = parser.parse_args(argv)

    if args.cmd == "add":
        path = quarantine(args.component, args.reason)
        print(path)
    elif args.cmd == "release":
        ok = release(args.component)
        print("released" if ok else "no-such-quarantine")
        return 0 if ok else 1
    elif args.cmd == "list":
        print(json.dumps(list_quarantined(), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
