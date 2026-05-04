#!/usr/bin/env python3
"""SENTINEL — credential_refresh.py (Phase C.2).

Validates configured API keys against the provider's auth endpoint and
emits an alert when a key is rejected. SENTINEL doesn't rotate provider
keys automatically — that requires the provider's own management API and
human approval — so "refresh" here means: detect a bad key, log loudly,
and notify the human via the alert channel.

Called by watchdog when the api-keys target reports unhealthy.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent))
sys.path.insert(0, str(_HERE.parent / "layer2-agent"))
sys.path.insert(0, str(_HERE))

from utils.common import (  # noqa: E402
    TASK_ALERT,
    iso_now,
    load_env_file,
    get_logger,
)
from utils.validator import validate_api_key  # noqa: E402

LOG = get_logger("credential-refresh")


def refresh() -> dict[str, str]:
    """Test each configured key. Returns {provider: status}."""
    env = load_env_file()
    statuses: dict[str, str] = {}
    bad: list[tuple[str, str]] = []

    for provider, env_var in (("anthropic", "ANTHROPIC_API_KEY"),
                              ("openai",    "OPENAI_API_KEY")):
        key = env.get(env_var, "")
        if not key:
            statuses[provider] = "not-configured"
            continue
        ok = validate_api_key(provider, key)
        statuses[provider] = "ok" if ok else "rejected"
        if not ok:
            bad.append((provider, env_var))
            LOG.error("API key for %s rejected by provider", provider)
        else:
            LOG.info("API key for %s validated", provider)

    if bad:
        _notify_human(bad)
    return statuses


def _notify_human(bad: list[tuple[str, str]]) -> None:
    """Best-effort alert through output_router — never raises."""
    try:
        import output_router  # type: ignore
    except Exception as exc:
        LOG.warning("output_router unavailable: %s", exc)
        return

    body_lines = ["The following SENTINEL API keys are no longer valid:", ""]
    for provider, env_var in bad:
        body_lines.append(f"  • {provider} ({env_var})")
    body_lines.extend([
        "",
        "Action required:",
        "  1. Replace the key in /opt/sentinel/.env",
        "  2. systemctl restart sentinel-agent",
        f"Detected at {iso_now()}",
    ])
    body = "\n".join(body_lines)

    task = SimpleNamespace(
        task_id=f"CRED-{iso_now()}",
        task_type=TASK_ALERT,
        description="API credential rejected by provider",
        priority=1,
    )
    result = SimpleNamespace(text=body, structured=None,
                             provider="watchdog", model="watchdog",
                             tokens_in=0, tokens_out=0)
    try:
        output_router.deliver(task, result, autonomous=True)
    except Exception as exc:
        LOG.error("alert delivery failed: %s", exc)


if __name__ == "__main__":
    print(refresh())
