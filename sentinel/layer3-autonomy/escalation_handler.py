#!/usr/bin/env python3
"""SENTINEL — escalation_handler.py (Phase C.1).

Route uncertain tasks to a human. The decision is based on:

  1. confidence score < ESCALATE_THRESHOLD            → escalate
  2. structured response with escalate=true (TASK_ESCALATE) → escalate
  3. priority == 1 (critical)                          → escalate

When escalation is required we:

  * append a record to data/task-history.json with status="escalated"
  * push an alert through output_router (slack/email per tasks.yaml)
  * leave the GAS row's status = quarantined (caller responsible for write)
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent))
sys.path.insert(0, str(_HERE.parent / "layer2-agent"))
sys.path.insert(0, str(_HERE))

from utils.common import (  # noqa: E402
    TASK_HISTORY_FILE,
    TASK_ESCALATE,
    TASK_ALERT,
    iso_now,
    read_json,
    write_json,
    get_logger,
)
from confidence_scorer import score as score_confidence  # noqa: E402

LOG = get_logger("escalation")

ESCALATE_THRESHOLD = 0.55


@dataclass
class EscalationDecision:
    escalate: bool
    severity: str
    confidence: float
    reason: str


def should_escalate(task, result) -> EscalationDecision:
    conf = score_confidence(task, result)

    if int(getattr(task, "priority", 3) or 3) <= 1:
        return EscalationDecision(True, "critical", conf.score, "priority<=1")

    if task.task_type == TASK_ESCALATE:
        struct = getattr(result, "structured", None) or {}
        if struct.get("escalate") is True:
            sev = str(struct.get("severity", "medium")).lower()
            return EscalationDecision(True, sev, conf.score, "model-flagged-escalate")

    if conf.score < ESCALATE_THRESHOLD:
        return EscalationDecision(True, "medium", conf.score,
                                  f"low-confidence:{conf.reason}")

    return EscalationDecision(False, "low", conf.score, conf.reason)


def escalate(task, result, decision: EscalationDecision) -> None:
    """Record + notify. Best-effort — never raises."""
    entry = {
        "task_id": task.task_id,
        "task_type": task.task_type,
        "priority": getattr(task, "priority", 3),
        "status": "escalated",
        "severity": decision.severity,
        "confidence": decision.confidence,
        "reason": decision.reason,
        "escalated_at": iso_now(),
    }
    history = read_json(TASK_HISTORY_FILE, default={"tasks": []})
    history.setdefault("tasks", []).append(entry)
    history["tasks"] = history["tasks"][-5000:]
    write_json(TASK_HISTORY_FILE, history)
    LOG.warning("escalated task=%s severity=%s confidence=%.2f reason=%s",
                task.task_id, decision.severity, decision.confidence, decision.reason)

    # Best-effort alert through the output router. Wrap the original result
    # in an "alert" envelope so it lands in slack/email per tasks.yaml.
    try:
        import output_router  # type: ignore  (sibling layer2-agent)
    except Exception as exc:
        LOG.warning("output_router unavailable, alert not sent: %s", exc)
        return

    alert_task = SimpleNamespace(
        task_id=task.task_id,
        task_type=TASK_ALERT,
        description=f"Escalation [{decision.severity}] from {task.task_id}: {decision.reason}",
        priority=getattr(task, "priority", 1),
    )
    alert_result = SimpleNamespace(
        text=getattr(result, "text", "")[:2000],
        structured=getattr(result, "structured", None),
        provider=getattr(result, "provider", ""),
        model=getattr(result, "model", ""),
        tokens_in=getattr(result, "tokens_in", 0),
        tokens_out=getattr(result, "tokens_out", 0),
    )
    try:
        output_router.deliver(alert_task, alert_result, autonomous=True)
    except Exception as exc:
        LOG.error("alert delivery failed: %s", exc)
