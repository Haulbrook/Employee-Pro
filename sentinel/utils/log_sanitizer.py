"""SENTINEL — log sanitizer (Gate-2 FIX 4 / CT-19).

Scrubs API keys, tokens, and secrets from log output before they hit disk.
Implements the regression-risk mitigation from Gate-2: a pattern allowlist
is applied first so timestamps and task IDs are never matched.
"""

from __future__ import annotations

import logging
import re

# Patterns that should never be redacted, even if they look key-ish.
WHITELIST_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\b\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z\b"),  # ISO-8601 UTC
    re.compile(r"\bT-\d{3,}\b"),                              # task IDs (T-001…)
    re.compile(r"\bSENTINEL-\d{2,}\b"),                       # instance IDs
)

# Patterns that, when matched, are replaced with `<redacted:LABEL>`.
# Order matters — most specific first.
SECRET_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"sk-ant-[A-Za-z0-9_\-]{20,}"),                "anthropic-key"),
    (re.compile(r"\bclaude-[A-Za-z0-9_\-]{20,}\b"),            "claude-token"),
    (re.compile(r"\bsk-[A-Za-z0-9]{20,}\b"),                   "openai-key"),
    (re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b"),                "google-key"),
    (re.compile(r"\bxox[baprs]-[A-Za-z0-9\-]{10,}\b"),         "slack-token"),
    (re.compile(r"\bghp_[A-Za-z0-9]{20,}\b"),                  "github-pat"),
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"),          "github-pat"),
    (re.compile(r"-----BEGIN [A-Z ]+PRIVATE KEY-----"),        "private-key"),
    (re.compile(r"\bBearer\s+[A-Za-z0-9_\-\.=]{16,}\b"),       "bearer-token"),
    (re.compile(
        r"(?i)(api[_-]?key|secret|password|token)\s*[:=]\s*['\"]?[A-Za-z0-9_\-\.=]{8,}['\"]?"
    ), "credential"),
)


def sanitize_log(message: str) -> str:
    """Return ``message`` with secrets replaced by ``<redacted:LABEL>`` markers.

    Whitelisted substrings (timestamps, task IDs, instance IDs) are protected
    before redaction runs by replacing them with placeholders, then restored.
    """
    if not message:
        return message

    # Protect whitelisted spans by swapping them for an unlikely placeholder.
    placeholders: dict[str, str] = {}
    out = message
    for idx, pat in enumerate(WHITELIST_PATTERNS):
        for match in pat.finditer(out):
            tag = f"\x00WL{idx}_{len(placeholders)}\x00"
            placeholders[tag] = match.group(0)
        out = pat.sub(lambda m, _idx=idx: _make_tag(m, _idx, placeholders), out)

    for pattern, label in SECRET_PATTERNS:
        out = pattern.sub(f"<redacted:{label}>", out)

    for tag, original in placeholders.items():
        out = out.replace(tag, original)
    return out


def _make_tag(match: re.Match[str], idx: int, store: dict[str, str]) -> str:
    tag = f"\x00WL{idx}_{len(store)}\x00"
    store[tag] = match.group(0)
    return tag


class SanitizingFormatter(logging.Formatter):
    """logging.Formatter that runs sanitize_log on the final message."""

    def format(self, record: logging.LogRecord) -> str:
        return sanitize_log(super().format(record))
