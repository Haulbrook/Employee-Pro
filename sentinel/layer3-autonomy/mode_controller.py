#!/usr/bin/env python3
"""SENTINEL — mode_controller.py (Phase C.1).

Manages the autonomy ladder. Transitions per the README spec:

    ASSISTED → AUTONOMOUS:
        50+ tasks completed AND error_rate < 5% AND escalation_rate < 10%

    AUTONOMOUS → ASSISTED (downgrade):
        error_rate > 15% OR 3 consecutive failures OR human override

Reads task history from data/task-history.json and writes the resulting mode
to data/state.json. Run as a one-shot from the watchdog or systemd timer.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent))
sys.path.insert(0, str(_HERE))

from utils.common import (  # noqa: E402
    STATE_FILE,
    TASK_HISTORY_FILE,
    MODE_ASSISTED,
    MODE_AUTONOMOUS,
    MODE_SHADOW,
    VALID_MODES,
    STATUS_COMPLETE,
    STATUS_FAILED,
    STATUS_QUARANTINED,
    iso_now,
    read_json,
    write_json,
    get_logger,
)

LOG = get_logger("mode-controller")

# Thresholds — mirror the spec exactly.
PROMOTE_MIN_TASKS         = 50
PROMOTE_MAX_ERROR_RATE    = 0.05   # < 5%
PROMOTE_MAX_ESCALATE_RATE = 0.10   # < 10%

DOWNGRADE_ERROR_RATE      = 0.15   # > 15%
DOWNGRADE_CONSECUTIVE_FAILS = 3


def _stats_from_history() -> dict[str, float]:
    history = read_json(TASK_HISTORY_FILE, default={"tasks": []})
    tasks = history.get("tasks", [])
    completed = sum(1 for t in tasks if t.get("status") == STATUS_COMPLETE)
    failed    = sum(1 for t in tasks if t.get("status") in (STATUS_FAILED, STATUS_QUARANTINED))
    escalated = sum(1 for t in tasks if t.get("status") == "escalated")
    total = completed + failed
    error_rate = (failed / total) if total else 0.0
    escalate_rate = (escalated / total) if total else 0.0

    consecutive_fails = 0
    for t in reversed(tasks):
        if t.get("status") in (STATUS_FAILED, STATUS_QUARANTINED):
            consecutive_fails += 1
        else:
            break

    return {
        "total": total,
        "completed": completed,
        "failed": failed,
        "escalated": escalated,
        "error_rate": error_rate,
        "escalate_rate": escalate_rate,
        "consecutive_fails": consecutive_fails,
    }


def decide_mode(current: str, stats: dict[str, float], *, override: str | None = None) -> tuple[str, str]:
    """Return (next_mode, reason)."""
    if override:
        if override.upper() in VALID_MODES:
            return override.upper(), f"human-override:{override.upper()}"
        return current, f"invalid-override:{override}"

    current = (current or MODE_ASSISTED).upper()

    if current == MODE_AUTONOMOUS:
        if stats["error_rate"] > DOWNGRADE_ERROR_RATE:
            return MODE_ASSISTED, (
                f"downgrade:error_rate={stats['error_rate']:.2%}"
                f">{DOWNGRADE_ERROR_RATE:.0%}"
            )
        if stats["consecutive_fails"] >= DOWNGRADE_CONSECUTIVE_FAILS:
            return MODE_ASSISTED, (
                f"downgrade:consecutive_failures={stats['consecutive_fails']}"
            )
        return MODE_AUTONOMOUS, "stable"

    if current == MODE_ASSISTED:
        if (
            stats["completed"] >= PROMOTE_MIN_TASKS
            and stats["error_rate"]    < PROMOTE_MAX_ERROR_RATE
            and stats["escalate_rate"] < PROMOTE_MAX_ESCALATE_RATE
        ):
            return MODE_AUTONOMOUS, (
                f"promote:tasks={stats['completed']}"
                f" err={stats['error_rate']:.2%}"
                f" esc={stats['escalate_rate']:.2%}"
            )
        return MODE_ASSISTED, "criteria-not-met"

    if current == MODE_SHADOW:
        # Spec defers SHADOW transition for v1; stay in SHADOW until human flips.
        return MODE_SHADOW, "shadow-only-via-override"

    return MODE_ASSISTED, f"unknown-current:{current}"


def evaluate(*, override: str | None = None) -> dict[str, object]:
    state = read_json(STATE_FILE, default={"mode": MODE_ASSISTED})
    current = (state.get("mode") or MODE_ASSISTED).upper()
    stats = _stats_from_history()
    next_mode, reason = decide_mode(current, stats, override=override)

    report = {
        "evaluated_at": iso_now(),
        "current": current,
        "next": next_mode,
        "reason": reason,
        "stats": stats,
    }
    if next_mode != current:
        LOG.info("mode transition: %s -> %s (%s)", current, next_mode, reason)
        state["mode"] = next_mode
        state["mode_changed_at"] = iso_now()
        state["mode_change_reason"] = reason
        write_json(STATE_FILE, state)
    else:
        LOG.info("mode stable: %s (%s)", current, reason)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="mode-controller")
    parser.add_argument("--set", choices=[m.lower() for m in VALID_MODES],
                        help="force the mode to a specific value (human override)")
    args = parser.parse_args(argv)
    report = evaluate(override=args.set)
    print(report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
