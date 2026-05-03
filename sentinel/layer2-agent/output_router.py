#!/usr/bin/env python3
"""SENTINEL — output_router.py (Phase B.4).

Routes AI engine results to one or more output channels:

    file   — Markdown written to data/outputs/<task_id>.md (always-on)
    email  — SMTP (uses SMTP_HOST/PORT/USER/PASSWORD/FROM from .env)
    slack  — Incoming webhook (SLACK_WEBHOOK_URL in .env)

In ASSISTED mode only the file channel runs. AUTONOMOUS mode runs every
channel that is mapped to the task's type via tasks.yaml (defaults are baked
into TASK_TYPE_DEFAULTS below so the router still works before Phase E).
"""

from __future__ import annotations

import json
import smtplib
import ssl
import sys
from email.message import EmailMessage
from pathlib import Path
from typing import Any, Iterable

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent))
sys.path.insert(0, str(_HERE))

from utils.common import (  # noqa: E402
    OUTPUTS_DIR,
    CONFIG_DIR,
    TASK_ALERT,
    TASK_REPORT,
    TASK_GENERATE,
    TASK_CLASSIFY,
    TASK_ANALYZE,
    TASK_ESCALATE,
    TASK_CUSTOM,
    iso_now,
    load_env_file,
    get_logger,
)

LOG = get_logger("output-router")

# Default routing in case tasks.yaml is missing (Phase E creates it).
TASK_TYPE_DEFAULTS: dict[str, list[str]] = {
    TASK_ALERT:    ["file", "slack"],
    TASK_REPORT:   ["file", "email"],
    TASK_GENERATE: ["file"],
    TASK_CLASSIFY: ["file"],
    TASK_ANALYZE:  ["file"],
    TASK_ESCALATE: ["file", "slack", "email"],
    TASK_CUSTOM:   ["file"],
}


# ---------- channel registry ------------------------------------------------

def _channels_for(task_type: str) -> list[str]:
    cfg_path = CONFIG_DIR / "tasks.yaml"
    if cfg_path.exists():
        try:
            import yaml
            with open(cfg_path, "r", encoding="utf-8") as fh:
                cfg = yaml.safe_load(fh) or {}
            routes = (cfg.get("routes") or {})
            if task_type in routes and isinstance(routes[task_type], list):
                return list(routes[task_type])
        except Exception as exc:
            LOG.warning("tasks.yaml unreadable: %s", exc)
    return TASK_TYPE_DEFAULTS.get(task_type, ["file"])


# ---------- file channel (always available) --------------------------------

def _render_template(name: str, ctx: dict[str, Any]) -> str:
    """Load a Markdown template from layer2-agent/templates/<name> and
    substitute ${var} placeholders. Falls back to body-only if template missing."""
    tpl_path = _HERE / "templates" / name
    if not tpl_path.exists():
        return ctx.get("body", "")
    text = tpl_path.read_text(encoding="utf-8")
    for key, value in ctx.items():
        text = text.replace("${" + key + "}", str(value))
    return text


def _write_file(task, result, *, task_type: str) -> Path:
    OUTPUTS_DIR.mkdir(parents=True, exist_ok=True)
    template_name = "alert.md" if task_type == TASK_ALERT else "report.md"
    rendered = _render_template(template_name, {
        "task_id":     task.task_id,
        "task_type":   task.task_type,
        "description": task.description,
        "priority":    task.priority,
        "timestamp":   iso_now(),
        "provider":    result.provider,
        "model":       result.model,
        "tokens_in":   result.tokens_in,
        "tokens_out":  result.tokens_out,
        "body":        result.text,
        "structured":  json.dumps(result.structured, indent=2) if result.structured else "",
    })
    out_path = OUTPUTS_DIR / f"{task.task_id}.md"
    out_path.write_text(rendered, encoding="utf-8")
    return out_path


# ---------- email channel ---------------------------------------------------

def _send_email(task, result, *, env: dict[str, str]) -> None:
    host = env.get("SMTP_HOST", "")
    if not host:
        LOG.info("email channel requested but SMTP_HOST unset — skipping")
        return
    port = int(env.get("SMTP_PORT", "587") or 587)
    user = env.get("SMTP_USER", "")
    password = env.get("SMTP_PASSWORD", "")
    from_addr = env.get("SMTP_FROM", user)
    to_addr = env.get("SMTP_TO", from_addr)
    if not from_addr or not to_addr:
        LOG.warning("SMTP_FROM/SMTP_TO unset, cannot send email")
        return

    msg = EmailMessage()
    msg["Subject"] = f"[SENTINEL] {task.task_type.upper()} — {task.task_id}"
    msg["From"] = from_addr
    msg["To"] = to_addr
    msg.set_content(result.text)

    ctx = ssl.create_default_context()
    if port == 465:
        with smtplib.SMTP_SSL(host, port, context=ctx, timeout=30) as smtp:
            if user:
                smtp.login(user, password)
            smtp.send_message(msg)
    else:
        with smtplib.SMTP(host, port, timeout=30) as smtp:
            smtp.ehlo()
            smtp.starttls(context=ctx)
            smtp.ehlo()
            if user:
                smtp.login(user, password)
            smtp.send_message(msg)
    LOG.info("emailed task=%s to=%s", task.task_id, to_addr)


# ---------- Slack channel ---------------------------------------------------

def _send_slack(task, result, *, env: dict[str, str]) -> None:
    url = env.get("SLACK_WEBHOOK_URL", "")
    if not url:
        LOG.info("slack channel requested but SLACK_WEBHOOK_URL unset — skipping")
        return
    import requests
    payload = {
        "text": f"*SENTINEL · {task.task_type.upper()} · {task.task_id}*\n{result.text[:2900]}"
    }
    resp = requests.post(url, json=payload, timeout=15)
    if resp.status_code >= 400:
        raise RuntimeError(f"slack webhook returned {resp.status_code}: {resp.text[:200]}")
    LOG.info("slacked task=%s", task.task_id)


# ---------- public API ------------------------------------------------------

def deliver(task, result, *, autonomous: bool = True,
            channels_override: Iterable[str] | None = None) -> dict[str, Any]:
    """Deliver ``result`` for ``task``. Returns a dict of {channel: status}.

    * file   — always written
    * other  — only when autonomous=True (ASSISTED mode = file-only)
    """
    env = load_env_file()
    requested = list(channels_override) if channels_override else _channels_for(task.task_type)
    if "file" not in requested:
        requested = ["file", *requested]

    if not autonomous:
        requested = ["file"]

    statuses: dict[str, Any] = {}
    for ch in requested:
        try:
            if ch == "file":
                path = _write_file(task, result, task_type=task.task_type)
                statuses["file"] = str(path)
            elif ch == "email":
                _send_email(task, result, env=env)
                statuses["email"] = "sent"
            elif ch == "slack":
                _send_slack(task, result, env=env)
                statuses["slack"] = "sent"
            else:
                LOG.warning("unknown channel: %s", ch)
                statuses[ch] = "unknown-channel"
        except Exception as exc:
            LOG.error("channel %s failed for task=%s: %s", ch, task.task_id, exc)
            statuses[ch] = f"error: {exc}"
    return statuses


# ---------- CLI for testing -------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    """Render a synthetic task to /data/outputs and print delivery status."""
    import argparse
    parser = argparse.ArgumentParser(prog="output-router")
    parser.add_argument("--task-id", default="T-CLI")
    parser.add_argument("--type", default=TASK_GENERATE)
    parser.add_argument("--text", required=True)
    parser.add_argument("--autonomous", action="store_true")
    args = parser.parse_args(argv)

    from types import SimpleNamespace
    task = SimpleNamespace(
        task_id=args.task_id,
        task_type=args.type,
        description="cli-driven",
        priority=3,
    )
    result = SimpleNamespace(
        text=args.text, structured=None,
        provider="cli", model="cli", tokens_in=0, tokens_out=0,
    )
    print(deliver(task, result, autonomous=args.autonomous))
    return 0


if __name__ == "__main__":
    sys.exit(main())
