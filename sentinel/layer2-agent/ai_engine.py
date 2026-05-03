#!/usr/bin/env python3
"""SENTINEL — ai_engine.py (Phase B.3).

Processes a validated task using Claude (default) or OpenAI. Implements the
four decision types from the spec:

    CLASSIFY  — Categorize incoming data (emails, requests, reports)
    GENERATE  — Create content (reports, summaries, responses)
    ANALYZE   — Process data and return insights
    ESCALATE  — Determine if human intervention is needed

Provider switching is controlled by ai.provider in sentinel.yaml or the
ANTHROPIC_API_KEY / OPENAI_API_KEY env vars (whichever is non-empty).

Rate-limiting is delegated to rate_limiter.TokenBucket (Phase B.5).
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent))
sys.path.insert(0, str(_HERE))

from utils.common import (  # noqa: E402
    CONFIG_DIR,
    TASK_CLASSIFY,
    TASK_GENERATE,
    TASK_ANALYZE,
    TASK_ESCALATE,
    TASK_REPORT,
    TASK_ALERT,
    TASK_CUSTOM,
    load_env_file,
    get_logger,
)
from utils.validator import ValidatedTask  # noqa: E402

LOG = get_logger("ai-engine")

# Model defaults — Anthropic naming used per the spec's "Claude/OpenAI" wording.
DEFAULT_ANTHROPIC_MODEL = "claude-sonnet-4-6"
DEFAULT_OPENAI_MODEL    = "gpt-4o-mini"

# System prompts per decision type. Kept minimal and bounded.
SYSTEM_PROMPTS: dict[str, str] = {
    TASK_CLASSIFY: (
        "You are SENTINEL's classifier. Given the user's input, return strict JSON: "
        '{"category": "<short label>", "confidence": <0.0-1.0>, "reason": "<one sentence>"}. '
        "No prose outside the JSON."
    ),
    TASK_GENERATE: (
        "You are SENTINEL's generator. Produce the requested content directly. "
        "No preamble, no meta-commentary. Use Markdown when structure helps."
    ),
    TASK_ANALYZE: (
        "You are SENTINEL's analyst. Given the data described, return strict JSON: "
        '{"summary": "...", "findings": ["..."], "recommendation": "...", "confidence": <0.0-1.0>}.'
        " No prose outside the JSON."
    ),
    TASK_ESCALATE: (
        "You are SENTINEL's escalation router. Decide whether the task requires a human. "
        'Return strict JSON: {"escalate": true|false, "severity": "low|medium|high|critical", '
        '"reason": "<one sentence>", "suggested_action": "<one sentence>"}.'
    ),
    TASK_REPORT: (
        "You are SENTINEL's reporter. Produce a concise daily-style report with sections: "
        "Summary, Highlights, Issues, Next Steps. Markdown."
    ),
    TASK_ALERT: (
        "You are SENTINEL's alert author. Produce a one-paragraph alert message suitable for "
        "Slack: include WHAT, WHEN, IMPACT, ACTION. No code fences."
    ),
    TASK_CUSTOM: (
        "You are SENTINEL's general-purpose assistant. Respond directly and concisely."
    ),
}


@dataclass
class AIResult:
    text: str
    structured: dict[str, Any] | None = None
    provider: str = ""
    model: str = ""
    tokens_in: int = 0
    tokens_out: int = 0
    raw: dict[str, Any] = field(default_factory=dict)


# ---------- config + provider selection -------------------------------------

def _ai_config() -> dict[str, Any]:
    cfg_path = CONFIG_DIR / "sentinel.yaml"
    if not cfg_path.exists():
        return {"provider": "anthropic"}
    try:
        import yaml
        with open(cfg_path, "r", encoding="utf-8") as fh:
            cfg = yaml.safe_load(fh) or {}
        return cfg.get("ai", {"provider": "anthropic"})
    except Exception:
        return {"provider": "anthropic"}


def _select_provider() -> tuple[str, str]:
    """Return (provider, api_key). Honours ai.provider in config, falls back
    to whichever key is set in the env, raises if neither is configured."""
    env = load_env_file()
    cfg = _ai_config()
    preferred = (cfg.get("provider") or "anthropic").lower()

    anthropic_key = env.get("ANTHROPIC_API_KEY", "")
    openai_key    = env.get("OPENAI_API_KEY", "")

    if preferred == "anthropic" and anthropic_key:
        return "anthropic", anthropic_key
    if preferred == "openai" and openai_key:
        return "openai", openai_key
    if anthropic_key:
        return "anthropic", anthropic_key
    if openai_key:
        return "openai", openai_key
    raise RuntimeError(
        "No AI provider configured. Set ANTHROPIC_API_KEY or OPENAI_API_KEY in /opt/sentinel/.env"
    )


# ---------- rate-limiter (B.5) ----------------------------------------------

_RATE_BUCKET = None  # singleton, populated on first call


def _rate_bucket():
    global _RATE_BUCKET
    if _RATE_BUCKET is not None:
        return _RATE_BUCKET
    try:
        import rate_limiter  # noqa  (sibling, sys.path-injected by task_queue)
        cfg = _ai_config()
        rl = cfg.get("rate_limit", {}) if isinstance(cfg, dict) else {}
        _RATE_BUCKET = rate_limiter.TokenBucket(
            capacity=int(rl.get("capacity", 10)),
            refill_per_second=float(rl.get("refill_per_second", 0.2)),
        )
    except Exception as exc:
        LOG.warning("rate_limiter unavailable, proceeding unlimited: %s", exc)
        _RATE_BUCKET = None
    return _RATE_BUCKET


# ---------- providers -------------------------------------------------------

def _call_anthropic(system: str, user: str, model: str, api_key: str,
                    *, max_tokens: int = 1024, temperature: float = 0.4) -> AIResult:
    try:
        from anthropic import Anthropic
    except ImportError as exc:
        raise RuntimeError("anthropic SDK not installed (pip install anthropic)") from exc

    client = Anthropic(api_key=api_key)
    msg = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        temperature=temperature,
        system=system,
        messages=[{"role": "user", "content": user}],
    )
    text = "".join(
        block.text for block in msg.content
        if getattr(block, "type", None) == "text"
    )
    return AIResult(
        text=text,
        provider="anthropic",
        model=model,
        tokens_in=getattr(msg.usage, "input_tokens", 0),
        tokens_out=getattr(msg.usage, "output_tokens", 0),
        raw={"id": getattr(msg, "id", "")},
    )


def _call_openai(system: str, user: str, model: str, api_key: str,
                 *, max_tokens: int = 1024, temperature: float = 0.4) -> AIResult:
    import requests
    resp = requests.post(
        "https://api.openai.com/v1/chat/completions",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json={
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "max_tokens": max_tokens,
            "temperature": temperature,
        },
        timeout=60,
    )
    resp.raise_for_status()
    data = resp.json()
    text = data["choices"][0]["message"]["content"]
    usage = data.get("usage", {})
    return AIResult(
        text=text,
        provider="openai",
        model=model,
        tokens_in=usage.get("prompt_tokens", 0),
        tokens_out=usage.get("completion_tokens", 0),
        raw={"id": data.get("id", "")},
    )


# ---------- post-processing -------------------------------------------------

def _extract_json(text: str) -> dict[str, Any] | None:
    """Best-effort JSON extraction from a model response."""
    text = text.strip()
    if not text:
        return None
    # Strip ```json ... ``` fences if present.
    if text.startswith("```"):
        first_nl = text.find("\n")
        if first_nl != -1:
            text = text[first_nl + 1:]
        if text.endswith("```"):
            text = text[: -3]
        text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # Find the first balanced {…} substring.
        depth = 0
        start = -1
        for i, ch in enumerate(text):
            if ch == "{":
                if depth == 0:
                    start = i
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0 and start != -1:
                    candidate = text[start:i + 1]
                    try:
                        return json.loads(candidate)
                    except json.JSONDecodeError:
                        return None
        return None


# ---------- public API ------------------------------------------------------

def process_task(task: ValidatedTask) -> AIResult:
    """Execute one task and return an AIResult. Raises on hard failure."""
    system = SYSTEM_PROMPTS.get(task.task_type, SYSTEM_PROMPTS[TASK_CUSTOM])
    user = task.description

    bucket = _rate_bucket()
    if bucket is not None:
        bucket.acquire(1)  # blocks until a token is available

    provider, api_key = _select_provider()
    cfg = _ai_config()

    if provider == "anthropic":
        model = cfg.get("anthropic_model", DEFAULT_ANTHROPIC_MODEL)
        max_tokens = int(cfg.get("max_tokens", 1024))
        temperature = float(cfg.get("temperature", 0.4))
        result = _call_anthropic(system, user, model, api_key,
                                 max_tokens=max_tokens, temperature=temperature)
    else:
        model = cfg.get("openai_model", DEFAULT_OPENAI_MODEL)
        max_tokens = int(cfg.get("max_tokens", 1024))
        temperature = float(cfg.get("temperature", 0.4))
        result = _call_openai(system, user, model, api_key,
                              max_tokens=max_tokens, temperature=temperature)

    # CLASSIFY / ANALYZE / ESCALATE expect strict JSON; parse opportunistically.
    if task.task_type in (TASK_CLASSIFY, TASK_ANALYZE, TASK_ESCALATE):
        result.structured = _extract_json(result.text)

    LOG.info("task=%s type=%s provider=%s model=%s in=%d out=%d",
             task.task_id, task.task_type, result.provider, result.model,
             result.tokens_in, result.tokens_out)
    return result


# ---------- CLI for testing -------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    import argparse
    parser = argparse.ArgumentParser(prog="ai-engine")
    parser.add_argument("--type", default=TASK_CUSTOM, choices=list(SYSTEM_PROMPTS.keys()))
    parser.add_argument("--task-id", default="T-CLI")
    parser.add_argument("description", nargs="+")
    args = parser.parse_args(argv)

    task = ValidatedTask(
        task_id=args.task_id,
        task_type=args.type,
        description=" ".join(args.description),
        priority=3,
    )
    result = process_task(task)
    print(result.text)
    if result.structured is not None:
        print("---structured---")
        print(json.dumps(result.structured, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
