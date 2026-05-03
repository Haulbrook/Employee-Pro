"""SENTINEL — input validation layer (Gate-2 FIX 5).

Functions:
    validate_task(task)    -> ValidatedTask         # required fields, sanitization, max-length
    validate_config(cfg)   -> list[str]             # returns error strings (empty if OK)
    validate_api_key(provider, key) -> bool         # tests auth endpoint
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from utils.common import (
    VALID_STATUSES,
    VALID_TASK_TYPES,
    STATUS_PENDING,
)

MAX_DESCRIPTION_BYTES = 10 * 1024  # 10 KB hard ceiling
MIN_PRIORITY = 1
MAX_PRIORITY = 5

_HTML_TAG_RE  = re.compile(r"<[^>]+>")
_CTRL_CHAR_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


class ValidationError(ValueError):
    """Raised when a payload fails validation."""


@dataclass
class ValidatedTask:
    task_id: str
    task_type: str
    description: str
    priority: int
    status: str = STATUS_PENDING
    assigned_to: str = ""
    created_at: str = ""
    completed_at: str = ""
    result: str = ""
    error: str = ""
    raw: dict[str, Any] = field(default_factory=dict)


def _strip_html_and_control(text: str) -> str:
    text = _HTML_TAG_RE.sub("", text)
    text = _CTRL_CHAR_RE.sub("", text)
    return text.strip()


def validate_task(task: dict[str, Any]) -> ValidatedTask:
    """Validate and normalize a task dict from the GAS queue.

    Required: task_id, task_type, description.
    Sanitizes description (HTML/control chars stripped, length capped at 10 KB).
    """
    if not isinstance(task, dict):
        raise ValidationError("task must be a dict")

    task_id = str(task.get("task_id") or "").strip()
    if not task_id:
        raise ValidationError("task_id is required")
    if not re.match(r"^[A-Za-z0-9_\-]{1,64}$", task_id):
        raise ValidationError(f"invalid task_id: {task_id!r}")

    task_type = str(task.get("task_type") or "").strip().lower()
    if task_type not in VALID_TASK_TYPES:
        raise ValidationError(
            f"invalid task_type {task_type!r}; allowed: {VALID_TASK_TYPES}"
        )

    description = _strip_html_and_control(str(task.get("description") or ""))
    if not description:
        raise ValidationError("description is required and must be non-empty after sanitization")
    if len(description.encode("utf-8")) > MAX_DESCRIPTION_BYTES:
        raise ValidationError(
            f"description exceeds {MAX_DESCRIPTION_BYTES} bytes"
        )

    try:
        priority = int(task.get("priority", 3))
    except (TypeError, ValueError) as exc:
        raise ValidationError("priority must be an integer 1-5") from exc
    if not (MIN_PRIORITY <= priority <= MAX_PRIORITY):
        raise ValidationError(f"priority {priority} out of range {MIN_PRIORITY}-{MAX_PRIORITY}")

    status = str(task.get("status") or STATUS_PENDING).strip().lower()
    if status not in VALID_STATUSES:
        raise ValidationError(f"invalid status {status!r}")

    return ValidatedTask(
        task_id=task_id,
        task_type=task_type,
        description=description,
        priority=priority,
        status=status,
        assigned_to=str(task.get("assigned_to") or ""),
        created_at=str(task.get("created_at") or ""),
        completed_at=str(task.get("completed_at") or ""),
        result=str(task.get("result") or ""),
        error=str(task.get("error") or ""),
        raw=task,
    )


# ---------- config validation -----------------------------------------------

REQUIRED_CONFIG_KEYS: tuple[str, ...] = (
    "instance_id",
    "queue",
    "ai",
    "modes",
    "self_heal",
    "dashboard",
)


def validate_config(cfg: dict[str, Any]) -> list[str]:
    """Return a list of human-readable error strings; empty list = valid."""
    errors: list[str] = []
    if not isinstance(cfg, dict):
        return ["config must be a mapping"]

    for key in REQUIRED_CONFIG_KEYS:
        if key not in cfg:
            errors.append(f"missing required key: {key}")

    queue = cfg.get("queue", {})
    if isinstance(queue, dict):
        max_depth = queue.get("max_depth")
        if max_depth is not None and (not isinstance(max_depth, int) or max_depth <= 0):
            errors.append("queue.max_depth must be a positive integer")
        poll = queue.get("poll_interval_seconds")
        if poll is not None and (not isinstance(poll, int) or poll <= 0):
            errors.append("queue.poll_interval_seconds must be a positive integer")
    else:
        errors.append("queue must be a mapping")

    ai = cfg.get("ai", {})
    if isinstance(ai, dict):
        provider = ai.get("provider")
        if provider not in ("anthropic", "openai", None):
            errors.append("ai.provider must be 'anthropic' or 'openai'")
    else:
        errors.append("ai must be a mapping")

    return errors


# ---------- API key validation ----------------------------------------------

def validate_api_key(provider: str, key: str, *, timeout: float = 10.0) -> bool:
    """Smoke-test an API key against the provider's auth endpoint.

    Returns True if the key authenticates successfully, False otherwise.
    Network errors return False — caller should surface that distinction.
    """
    if not key:
        return False
    try:
        import requests
    except ImportError:
        return False

    try:
        if provider == "anthropic":
            resp = requests.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": "claude-haiku-4-5-20251001",
                    "max_tokens": 1,
                    "messages": [{"role": "user", "content": "ping"}],
                },
                timeout=timeout,
            )
            # 200 = success, 400/422 = bad request but key was accepted, 401/403 = bad key.
            return resp.status_code not in (401, 403)
        elif provider == "openai":
            resp = requests.get(
                "https://api.openai.com/v1/models",
                headers={"Authorization": f"Bearer {key}"},
                timeout=timeout,
            )
            return resp.status_code not in (401, 403)
        else:
            return False
    except Exception:
        return False
