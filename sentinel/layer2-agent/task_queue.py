#!/usr/bin/env python3
"""SENTINEL — task_queue.py (Phase B.2).

The main agent loop. Polls the GAS task queue, validates, dispatches to the
AI engine, routes outputs, and submits results back. Offline-first: caches
work locally if GAS is unreachable and syncs on reconnect.

Implements:
  * Gate-2 FIX 5  validator.validate_task() on every incoming row
  * Gate-2 FIX 4  log_sanitizer applied to all log output (via common.get_logger)
  * Gate-2 FIX 6  POST-body shared secret to GAS
  * CT-07 fix     queue depth ceiling (max_depth)
  * CT-13 fix     offline retry budget (offline_max_retries)
  * CT-15 fix     per-task retry exponential backoff before quarantine
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import time
from pathlib import Path
from typing import Any

# The README file tree uses hyphenated directory names (`layer2-agent/`),
# which can't be Python packages. We add the SENTINEL root *and* the layer
# directory itself to sys.path so utils/* imports resolve and same-layer
# siblings (ai_engine, output_router, rate_limiter) can be imported by name.
_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent))  # /opt/sentinel  → utils, etc.
sys.path.insert(0, str(_HERE))         # /opt/sentinel/layer2-agent → siblings

from utils.common import (  # noqa: E402
    SENTINEL_HOME,
    TASK_CACHE_FILE,
    TASK_HISTORY_FILE,
    STATE_FILE,
    CONFIG_DIR,
    MODE_SHADOW,
    MODE_ASSISTED,
    MODE_AUTONOMOUS,
    STATUS_COMPLETE,
    STATUS_FAILED,
    STATUS_QUARANTINED,
    iso_now,
    read_json,
    write_json,
    load_env_file,
    get_logger,
)
from utils.validator import validate_task, ValidationError, ValidatedTask  # noqa: E402

LOG = get_logger("task-queue")

DEFAULT_CONFIG = {
    "instance_id": "SENTINEL-01",
    "queue": {
        "poll_interval_seconds": 60,
        "max_depth": 100,
        "max_retries_per_task": 3,
        "offline_max_retries": 10,
        "offline_retry_interval": 300,
        "request_timeout_seconds": 30,
    },
    "ai": {"provider": "anthropic"},
    "modes": {"default": MODE_ASSISTED},
}


# ---------- config loading --------------------------------------------------

def load_config() -> dict[str, Any]:
    """Load sentinel.yaml if present, else return defaults. Phase E writes the
    real config; before then we fall back so the agent still runs."""
    cfg_path = CONFIG_DIR / "sentinel.yaml"
    if not cfg_path.exists():
        return dict(DEFAULT_CONFIG)
    try:
        import yaml  # type: ignore
    except ImportError:
        LOG.warning("PyYAML not installed; using default config")
        return dict(DEFAULT_CONFIG)
    try:
        with open(cfg_path, "r", encoding="utf-8") as fh:
            cfg = yaml.safe_load(fh) or {}
    except Exception as exc:
        LOG.error("failed to parse %s: %s; using defaults", cfg_path, exc)
        return dict(DEFAULT_CONFIG)
    # shallow-merge defaults
    merged = dict(DEFAULT_CONFIG)
    for k, v in cfg.items():
        if isinstance(v, dict) and isinstance(merged.get(k), dict):
            merged[k] = {**merged[k], **v}
        else:
            merged[k] = v
    return merged


# ---------- GAS client ------------------------------------------------------

class GasClient:
    """Thin wrapper around the GAS web-app endpoints."""

    def __init__(self, endpoint: str, secret: str, timeout: float = 30.0):
        self.endpoint = endpoint
        self.secret = secret
        self.timeout = timeout

    def _post(self, payload: dict[str, Any]) -> dict[str, Any]:
        if not self.endpoint or not self.secret:
            raise RuntimeError("GAS_ENDPOINT_URL or SENTINEL_SECRET not configured")
        import requests
        body = dict(payload, secret=self.secret)
        resp = requests.post(self.endpoint, json=body, timeout=self.timeout, allow_redirects=True)
        resp.raise_for_status()
        try:
            data = resp.json()
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"non-JSON response from GAS: {resp.text[:200]}") from exc
        # GAS encodes its logical status in body.http_code (always 200 envelope).
        code = data.get("http_code", 200)
        if code == 403:
            raise PermissionError("GAS rejected shared secret (403)")
        if code >= 400:
            raise RuntimeError(f"GAS error {code}: {data.get('error')}")
        return data

    def get_next_task(self, instance_id: str) -> dict[str, Any] | None:
        data = self._post({"action": "getNextTask", "instance_id": instance_id})
        return data.get("task")

    def submit_result(
        self,
        task_id: str,
        result: str,
        status: str = STATUS_COMPLETE,
        error: str = "",
    ) -> dict[str, Any]:
        return self._post({
            "action": "submitResult",
            "task_id": task_id,
            "result": result,
            "status": status,
            "error": error,
        })

    def get_status(self) -> dict[str, Any]:
        return self._post({"action": "getStatus"})


# ---------- offline cache ---------------------------------------------------

def _load_cache() -> dict[str, Any]:
    return read_json(TASK_CACHE_FILE, default={"pending_results": [], "saved_tasks": []})


def _save_cache(cache: dict[str, Any]) -> None:
    write_json(TASK_CACHE_FILE, cache)


def cache_unsubmitted_result(task_id: str, result: str, status: str, error: str) -> None:
    cache = _load_cache()
    cache.setdefault("pending_results", []).append({
        "task_id": task_id,
        "result": result,
        "status": status,
        "error": error,
        "queued_at": iso_now(),
        "attempts": 0,
    })
    _save_cache(cache)
    LOG.warning("cached result locally (offline): task=%s status=%s", task_id, status)


def flush_cached_results(client: GasClient, max_attempts: int) -> int:
    cache = _load_cache()
    pending = cache.get("pending_results", [])
    if not pending:
        return 0
    remaining: list[dict[str, Any]] = []
    flushed = 0
    for item in pending:
        if item.get("attempts", 0) >= max_attempts:
            LOG.error("dropping cached result after %d attempts: task=%s",
                      max_attempts, item.get("task_id"))
            continue
        try:
            client.submit_result(
                item["task_id"], item["result"], item["status"], item.get("error", "")
            )
            flushed += 1
        except Exception as exc:
            item["attempts"] = item.get("attempts", 0) + 1
            item["last_error"] = str(exc)
            remaining.append(item)
    cache["pending_results"] = remaining
    _save_cache(cache)
    if flushed:
        LOG.info("flushed %d cached results to GAS", flushed)
    return flushed


# ---------- history + state -------------------------------------------------

def append_history(entry: dict[str, Any]) -> None:
    history = read_json(TASK_HISTORY_FILE, default={"tasks": []})
    history.setdefault("tasks", []).append(entry)
    # Cap history to 5000 entries so the file stays bounded.
    history["tasks"] = history["tasks"][-5000:]
    write_json(TASK_HISTORY_FILE, history)


def update_state(**fields: Any) -> dict[str, Any]:
    state = read_json(STATE_FILE, default={
        "instance_id": os.environ.get("SENTINEL_INSTANCE_ID", "SENTINEL-01"),
        "mode": MODE_ASSISTED,
        "started_at": None,
        "last_task_at": None,
        "tasks_completed": 0,
        "tasks_failed": 0,
        "tasks_escalated": 0,
        "quarantined_components": [],
    })
    state.update(fields)
    write_json(STATE_FILE, state)
    return state


# ---------- task processing -------------------------------------------------

def process_task(task: ValidatedTask, mode: str, ai_module, output_module) -> tuple[str, str, str]:
    """Return (status, result, error). Pure dispatch — caller persists outcome.

    * SHADOW mode      : log only, mark complete with empty result
    * ASSISTED mode    : run AI, deliver to local file only (no external send)
    * AUTONOMOUS mode  : run AI, deliver via all configured channels
    """
    if mode == MODE_SHADOW:
        LOG.info("SHADOW mode: not executing task=%s type=%s", task.task_id, task.task_type)
        return STATUS_COMPLETE, "[shadow-mode: not executed]", ""

    try:
        ai_result = ai_module.process_task(task)
    except Exception as exc:
        LOG.error("ai-engine failure on task=%s: %s", task.task_id, exc)
        return STATUS_FAILED, "", f"ai-engine: {exc}"

    try:
        output_module.deliver(task, ai_result, autonomous=(mode == MODE_AUTONOMOUS))
    except Exception as exc:
        LOG.error("output-router failure on task=%s: %s", task.task_id, exc)
        return STATUS_FAILED, ai_result.text, f"output-router: {exc}"

    return STATUS_COMPLETE, ai_result.text, ""


# ---------- main loop -------------------------------------------------------

def run_loop(args: argparse.Namespace) -> None:
    cfg = load_config()
    env = load_env_file()
    instance_id = env.get("SENTINEL_INSTANCE_ID", cfg.get("instance_id", "SENTINEL-01"))
    poll_interval = int(cfg["queue"]["poll_interval_seconds"])
    offline_interval = int(cfg["queue"]["offline_retry_interval"])
    offline_max_retries = int(cfg["queue"]["offline_max_retries"])
    max_retries_per_task = int(cfg["queue"]["max_retries_per_task"])
    request_timeout = float(cfg["queue"]["request_timeout_seconds"])
    queue_max_depth = int(cfg["queue"]["max_depth"])

    client = GasClient(
        endpoint=env.get("GAS_ENDPOINT_URL", ""),
        secret=env.get("SENTINEL_SECRET", ""),
        timeout=request_timeout,
    )

    # Lazy-load processor + router so missing deps don't crash bootstrap-time imports.
    import ai_engine as ai_module        # noqa: E401  (sibling, sys.path-injected)
    import output_router as output_module  # noqa: E401

    update_state(started_at=iso_now())
    LOG.info("agent started: instance=%s poll_interval=%ds", instance_id, poll_interval)

    running = True
    def _stop(signum, _frame):  # noqa: ARG001
        nonlocal running
        LOG.info("received signal %s, draining and exiting", signum)
        running = False
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    consecutive_offline = 0

    while running:
        cycle_start = time.time()

        # 1) Flush any cached results from offline windows.
        try:
            flush_cached_results(client, offline_max_retries)
            consecutive_offline = 0
        except Exception as exc:
            consecutive_offline += 1
            LOG.warning("offline (cycle %d): %s", consecutive_offline, exc)

        # 2) Read current state to honor mode + quarantine flags.
        state = read_json(STATE_FILE, default={"mode": MODE_ASSISTED})
        mode = str(state.get("mode") or MODE_ASSISTED).upper()

        # 3) Honor queue depth ceiling (CT-07): if local history's pending
        #    in-flight count exceeds max_depth, sleep before pulling more.
        in_flight = len([t for t in read_json(TASK_HISTORY_FILE, {"tasks": []}).get("tasks", [])
                         if t.get("status") == "processing"])
        if in_flight >= queue_max_depth:
            LOG.warning("queue depth %d >= max %d, backing off", in_flight, queue_max_depth)
            time.sleep(poll_interval)
            continue

        # 4) Pull next task.
        task_dict = None
        try:
            task_dict = client.get_next_task(instance_id)
            consecutive_offline = 0
        except PermissionError:
            LOG.error("GAS rejected our secret — check SENTINEL_SECRET. Sleeping 5 min.")
            time.sleep(300)
            continue
        except Exception as exc:
            consecutive_offline += 1
            LOG.warning("GAS unreachable (offline cycle %d): %s", consecutive_offline, exc)

        if task_dict is None:
            sleep_for = offline_interval if consecutive_offline > 0 else poll_interval
            time.sleep(sleep_for)
            continue

        # 5) Validate.
        try:
            task = validate_task(task_dict)
        except ValidationError as exc:
            LOG.error("invalid task from GAS: %s rejected (%s)", task_dict.get("task_id"), exc)
            try:
                client.submit_result(
                    task_dict.get("task_id", "unknown"),
                    result="",
                    status=STATUS_FAILED,
                    error=f"validation: {exc}",
                )
            except Exception:
                pass
            continue

        # 6) Execute, with in-task retries before declaring failed.
        attempts = 0
        last_error = ""
        status = STATUS_FAILED
        result_text = ""
        while attempts < max_retries_per_task:
            attempts += 1
            status, result_text, last_error = process_task(task, mode, ai_module, output_module)
            if status == STATUS_COMPLETE:
                break
            backoff = min(60, 5 * (2 ** (attempts - 1)))
            LOG.info("task=%s attempt %d/%d failed: %s (sleep %ds)",
                     task.task_id, attempts, max_retries_per_task, last_error, backoff)
            time.sleep(backoff)
        if status != STATUS_COMPLETE and attempts >= max_retries_per_task:
            status = STATUS_QUARANTINED
            LOG.error("task=%s quarantined after %d failures", task.task_id, attempts)

        # 7) Submit result (or cache offline).
        try:
            client.submit_result(task.task_id, result_text, status, last_error)
        except Exception as exc:
            LOG.warning("could not submit result, caching: %s", exc)
            cache_unsubmitted_result(task.task_id, result_text, status, last_error)

        # 8) Persist history + state counters.
        append_history({
            "task_id": task.task_id,
            "task_type": task.task_type,
            "priority": task.priority,
            "status": status,
            "attempts": attempts,
            "started_at": iso_now(),
            "completed_at": iso_now(),
            "error": last_error,
        })
        if status == STATUS_COMPLETE:
            update_state(last_task_at=iso_now(),
                         tasks_completed=int(state.get("tasks_completed", 0)) + 1)
        elif status == STATUS_QUARANTINED:
            update_state(last_task_at=iso_now(),
                         tasks_failed=int(state.get("tasks_failed", 0)) + 1)
        else:
            update_state(last_task_at=iso_now(),
                         tasks_failed=int(state.get("tasks_failed", 0)) + 1)

        # Don't sleep the full interval if we just got work — pull again sooner.
        elapsed = time.time() - cycle_start
        sleep_for = max(1.0, min(poll_interval, poll_interval - elapsed))
        if running:
            time.sleep(sleep_for)

    LOG.info("agent stopped cleanly")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="task-queue")
    parser.add_argument("--once", action="store_true",
                        help="poll once and exit (used by tests)")
    args = parser.parse_args(argv)
    if args.once:
        # one-shot variant for testing: do a single cycle then return
        cfg = load_config()
        env = load_env_file()
        client = GasClient(
            endpoint=env.get("GAS_ENDPOINT_URL", ""),
            secret=env.get("SENTINEL_SECRET", ""),
            timeout=float(cfg["queue"]["request_timeout_seconds"]),
        )
        flush_cached_results(client, int(cfg["queue"]["offline_max_retries"]))
        try:
            task_dict = client.get_next_task(env.get("SENTINEL_INSTANCE_ID", "SENTINEL-01"))
        except Exception as exc:
            LOG.warning("once-cycle: %s", exc)
            return 1
        if task_dict is None:
            LOG.info("no task available")
            return 0
        LOG.info("got task: %s", task_dict.get("task_id"))
        return 0
    run_loop(args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
