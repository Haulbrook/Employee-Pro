#!/usr/bin/env python3
"""SENTINEL — confidence_scorer.py (Phase C.1).

Score the confidence of a single task execution. Inputs:

  * provider's own confidence (when present in result.structured)
  * provider failure indicators (empty text, JSON-parse failure for typed tasks)
  * priority / type heuristics (high-severity escalations get capped lower)

Output: a float in [0.0, 1.0] plus a reason string.

Used by the escalation handler to decide whether to ask for human review.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from utils.common import (
    TASK_CLASSIFY,
    TASK_ANALYZE,
    TASK_ESCALATE,
)

# Tasks where we expect a structured JSON response. If parsing failed, that
# signals the model didn't comply, which is a confidence hit.
STRUCTURED_TYPES = {TASK_CLASSIFY, TASK_ANALYZE, TASK_ESCALATE}


@dataclass
class Confidence:
    score: float
    reason: str


def score(task, result) -> Confidence:
    """Return a Confidence for the (task, AIResult) pair.

    Both arguments are duck-typed: ``task`` needs ``task_type``/``priority``;
    ``result`` needs ``text``/``structured``.
    """
    reasons: list[str] = []
    s = 0.85  # baseline for a non-empty success

    text = (getattr(result, "text", "") or "").strip()
    if not text:
        return Confidence(0.0, "empty-text")

    structured: dict[str, Any] | None = getattr(result, "structured", None)

    if task.task_type in STRUCTURED_TYPES:
        if structured is None:
            s -= 0.40
            reasons.append("expected-json-but-none")
        else:
            # Provider gave its own confidence — trust it as a strong signal.
            provider_conf = structured.get("confidence")
            if isinstance(provider_conf, (int, float)) and 0.0 <= float(provider_conf) <= 1.0:
                s = 0.5 * s + 0.5 * float(provider_conf)
                reasons.append(f"provider_conf={float(provider_conf):.2f}")
            # Escalation flagged → cap confidence lower (human will look anyway).
            if task.task_type == TASK_ESCALATE and structured.get("escalate") is True:
                s = min(s, 0.6)
                reasons.append("escalate=true")

    # Token output too short for a generate task is a smell.
    if task.task_type not in STRUCTURED_TYPES and len(text) < 32:
        s -= 0.20
        reasons.append("very-short-output")

    # High-priority tasks (1 = critical) deserve a confidence cap so we always
    # think twice before going fully autonomous on them.
    priority = int(getattr(task, "priority", 3) or 3)
    if priority <= 1:
        s = min(s, 0.85)
        reasons.append("priority<=1")

    s = max(0.0, min(1.0, s))
    return Confidence(s, ";".join(reasons) if reasons else "default")
