#!/usr/bin/env python3
"""SENTINEL — network_sentinel.py (Phase C.4).

Connectivity state machine + retry-budget enforcement.

States:
    ONLINE       both ping + DNS healthy
    DEGRADED     one of the two failing for <= DEGRADED_TICKS cycles
    OFFLINE      degraded for too long, or both failing
    RECOVERING   was OFFLINE, now ok — needs RECOVERY_TICKS healthy cycles
                 in a row to flip back to ONLINE (CT-13: avoid retry storms)

Outputs:
    data/network-state.json with current state, transitions, retry stats.
    Other modules (task_queue.py) call ``is_online()`` before submitting.
"""

from __future__ import annotations

import argparse
import shutil
import socket
import subprocess
import sys
import time
from dataclasses import dataclass, asdict
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent))

from utils.common import (  # noqa: E402
    DATA_DIR,
    iso_now,
    read_json,
    write_json,
    get_logger,
)

LOG = get_logger("network-sentinel")

NETWORK_STATE_FILE = DATA_DIR / "network-state.json"

CYCLE_SECONDS    = 15
DEGRADED_TICKS   = 3   # cycles in DEGRADED before declaring OFFLINE
RECOVERY_TICKS   = 2   # cycles healthy in a row before going ONLINE again
PING_TARGET      = "8.8.8.8"
DNS_TARGET       = "google.com"

STATE_ONLINE     = "ONLINE"
STATE_DEGRADED   = "DEGRADED"
STATE_OFFLINE    = "OFFLINE"
STATE_RECOVERING = "RECOVERING"


@dataclass
class Snapshot:
    state: str
    ping_ok: bool
    dns_ok: bool
    degraded_ticks: int
    healthy_ticks: int
    offline_since: str | None
    transitions: int
    last_change_at: str | None
    last_change_reason: str
    updated_at: str


# ---------- probes ----------------------------------------------------------

def ping_ok(target: str = PING_TARGET, timeout: int = 2) -> bool:
    if not shutil.which("ping"):
        return False
    try:
        return subprocess.run(
            ["ping", "-c", "1", "-W", str(timeout), target],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=timeout + 1,
        ).returncode == 0
    except Exception:
        return False


def dns_ok(target: str = DNS_TARGET) -> bool:
    try:
        socket.gethostbyname(target)
        return True
    except Exception:
        return False


# ---------- state I/O -------------------------------------------------------

def _load() -> Snapshot:
    raw = read_json(NETWORK_STATE_FILE, default=None)
    if not raw:
        return Snapshot(
            state=STATE_ONLINE, ping_ok=True, dns_ok=True,
            degraded_ticks=0, healthy_ticks=0, offline_since=None,
            transitions=0, last_change_at=None, last_change_reason="init",
            updated_at=iso_now(),
        )
    return Snapshot(**{k: raw.get(k) for k in Snapshot.__dataclass_fields__})


def _save(snap: Snapshot) -> None:
    snap.updated_at = iso_now()
    write_json(NETWORK_STATE_FILE, asdict(snap))


def _transition(snap: Snapshot, new_state: str, reason: str) -> None:
    if snap.state == new_state:
        return
    LOG.info("network state %s → %s (%s)", snap.state, new_state, reason)
    snap.state = new_state
    snap.transitions += 1
    snap.last_change_at = iso_now()
    snap.last_change_reason = reason
    if new_state == STATE_OFFLINE and not snap.offline_since:
        snap.offline_since = iso_now()
    if new_state == STATE_ONLINE:
        snap.offline_since = None


# ---------- state machine ---------------------------------------------------

def step(snap: Snapshot, *, p_ok: bool | None = None, d_ok: bool | None = None) -> Snapshot:
    """One tick of the state machine. ``p_ok``/``d_ok`` allow tests to inject."""
    p = p_ok if p_ok is not None else ping_ok()
    d = d_ok if d_ok is not None else dns_ok()
    snap.ping_ok = p
    snap.dns_ok = d

    healthy = p and d

    if snap.state == STATE_ONLINE:
        if healthy:
            snap.healthy_ticks = 0
        elif p or d:
            snap.degraded_ticks = 1
            _transition(snap, STATE_DEGRADED, "partial-failure")
        else:
            snap.degraded_ticks = 0
            _transition(snap, STATE_OFFLINE, "ping+dns-down")

    elif snap.state == STATE_DEGRADED:
        if healthy:
            snap.degraded_ticks = 0
            snap.healthy_ticks = 1
            _transition(snap, STATE_RECOVERING, "partial-recovery")
        elif not p and not d:
            _transition(snap, STATE_OFFLINE, "degraded->offline")
            snap.degraded_ticks = 0
        else:
            snap.degraded_ticks += 1
            if snap.degraded_ticks >= DEGRADED_TICKS:
                _transition(snap, STATE_OFFLINE, "degraded-too-long")
                snap.degraded_ticks = 0

    elif snap.state == STATE_OFFLINE:
        if healthy:
            snap.healthy_ticks = 1
            _transition(snap, STATE_RECOVERING, "first-healthy-tick")
        else:
            # remain OFFLINE; offline_since stays, no retry storm
            pass

    elif snap.state == STATE_RECOVERING:
        if healthy:
            snap.healthy_ticks += 1
            if snap.healthy_ticks >= RECOVERY_TICKS:
                snap.healthy_ticks = 0
                _transition(snap, STATE_ONLINE, "fully-recovered")
        else:
            snap.healthy_ticks = 0
            _transition(snap, STATE_OFFLINE if not (p or d) else STATE_DEGRADED,
                        "recovery-aborted")
            if snap.state == STATE_DEGRADED:
                snap.degraded_ticks = 1

    _save(snap)
    return snap


# ---------- public helpers --------------------------------------------------

def is_online() -> bool:
    """Cheap state lookup for callers that don't run the loop themselves."""
    snap = _load()
    return snap.state == STATE_ONLINE


def current() -> dict:
    return asdict(_load())


# ---------- runner ----------------------------------------------------------

def run_loop() -> None:
    snap = _load()
    LOG.info("network-sentinel starting (initial state=%s)", snap.state)
    while True:
        try:
            step(snap)
        except Exception as exc:
            LOG.error("step raised: %s", exc)
        time.sleep(CYCLE_SECONDS)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="network-sentinel")
    parser.add_argument("--once", action="store_true",
                        help="run a single state-machine tick and exit")
    parser.add_argument("--show", action="store_true",
                        help="print current state and exit")
    args = parser.parse_args(argv)

    if args.show:
        import json
        print(json.dumps(current(), indent=2))
        return 0

    if args.once:
        snap = step(_load())
        print(f"{snap.state} ping={snap.ping_ok} dns={snap.dns_ok}")
        return 0

    run_loop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
