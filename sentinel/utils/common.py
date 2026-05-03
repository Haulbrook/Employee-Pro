"""SENTINEL shared constants, paths, and small helpers."""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# ---------- paths -----------------------------------------------------------

SENTINEL_HOME = Path(os.environ.get("SENTINEL_HOME", "/opt/sentinel"))
CONFIG_DIR    = SENTINEL_HOME / "config"
DATA_DIR      = SENTINEL_HOME / "data"
LOG_DIR       = Path(os.environ.get("SENTINEL_LOG_DIR", "/var/log/sentinel"))

STATE_FILE        = DATA_DIR / "state.json"
METRICS_FILE      = DATA_DIR / "metrics.json"
TASK_HISTORY_FILE = DATA_DIR / "task-history.json"
TASK_CACHE_FILE   = DATA_DIR / "task-cache.json"
QUARANTINE_DIR    = DATA_DIR / "quarantine"
OUTPUTS_DIR       = DATA_DIR / "outputs"

ENV_FILE = SENTINEL_HOME / ".env"

# ---------- modes (Layer 3) -------------------------------------------------

MODE_SHADOW     = "SHADOW"
MODE_ASSISTED   = "ASSISTED"
MODE_AUTONOMOUS = "AUTONOMOUS"
VALID_MODES = (MODE_SHADOW, MODE_ASSISTED, MODE_AUTONOMOUS)

# ---------- task statuses (Layer 2) -----------------------------------------

STATUS_PENDING     = "pending"
STATUS_PROCESSING  = "processing"
STATUS_COMPLETE    = "complete"
STATUS_FAILED      = "failed"
STATUS_QUARANTINED = "quarantined"
VALID_STATUSES = (
    STATUS_PENDING, STATUS_PROCESSING, STATUS_COMPLETE,
    STATUS_FAILED, STATUS_QUARANTINED,
)

# ---------- task types ------------------------------------------------------

TASK_CLASSIFY = "classify"
TASK_GENERATE = "generate"
TASK_ANALYZE  = "analyze"
TASK_ESCALATE = "escalate"
TASK_REPORT   = "report"
TASK_ALERT    = "alert"
TASK_CUSTOM   = "custom"
VALID_TASK_TYPES = (
    TASK_CLASSIFY, TASK_GENERATE, TASK_ANALYZE,
    TASK_ESCALATE, TASK_REPORT, TASK_ALERT, TASK_CUSTOM,
)

# ---------- helpers ---------------------------------------------------------

def iso_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def utc_timestamp() -> float:
    return time.time()


def read_json(path: Path, default: Any = None) -> Any:
    """Read JSON file, returning ``default`` if missing or unreadable."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        return default
    except json.JSONDecodeError:
        return default


def write_json(path: Path, payload: Any) -> None:
    """Atomic JSON write via temp file + rename."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, sort_keys=True)
    os.replace(tmp, path)


def load_env_file(path: Path = ENV_FILE) -> dict[str, str]:
    """Tiny .env loader. Does not support multi-line values or interpolation."""
    env: dict[str, str] = {}
    if not path.exists():
        return env
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
            value = value[1:-1]
        env[key] = value
    return env


def get_logger(name: str) -> logging.Logger:
    """Return a logger that writes to LOG_DIR/<name>.log AND stdout, with sanitization."""
    from utils.log_sanitizer import SanitizingFormatter

    logger = logging.getLogger(name)
    if logger.handlers:
        return logger
    logger.setLevel(logging.INFO)

    fmt = SanitizingFormatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%SZ",
    )

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    try:
        fh = logging.FileHandler(LOG_DIR / f"{name}.log")
        fh.setFormatter(fmt)
        logger.addHandler(fh)
    except (PermissionError, FileNotFoundError):
        pass

    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    logger.addHandler(sh)
    return logger
