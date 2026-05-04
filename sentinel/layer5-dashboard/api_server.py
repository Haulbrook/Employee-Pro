#!/usr/bin/env python3
"""SENTINEL — api_server.py (Phase D.2).

Localhost-only HTTP server that serves the dashboard static files and a
JSON API consumed by dashboard.js.

Security (Gate-2 FIX 7 / CT-17):
  * Binds explicitly to 127.0.0.1:8501 (never 0.0.0.0)
  * X-Content-Type-Options: nosniff on every response
  * X-Frame-Options: DENY
  * Referrer-Policy: same-origin
  * Cache-Control: no-store on /api/*

API:
  GET /api/status     instance + mode + uptime + watchdog snapshot
  GET /api/tasks      recent task history (newest first, capped)
  GET /api/metrics    last health-monitor.sh sample
  GET /api/alerts     recent escalations + quarantine events
  GET /              dashboard index.html
  GET /<asset>        dashboard.js, style.css, etc. (whitelisted MIME)
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent))

from utils.common import (  # noqa: E402
    METRICS_FILE,
    STATE_FILE,
    TASK_HISTORY_FILE,
    DATA_DIR,
    iso_now,
    read_json,
    get_logger,
)

LOG = get_logger("dashboard-api")

BIND_HOST = "127.0.0.1"
BIND_PORT = 8501
STATIC_DIR = _HERE
ALLOWED_STATIC = {"index.html", "dashboard.js", "style.css", "favicon.ico"}

NETWORK_STATE_FILE = DATA_DIR / "network-state.json"


def _security_headers(handler: BaseHTTPRequestHandler) -> None:
    handler.send_header("X-Content-Type-Options", "nosniff")
    handler.send_header("X-Frame-Options", "DENY")
    handler.send_header("Referrer-Policy", "same-origin")


def _send_json(handler: BaseHTTPRequestHandler, payload, *, status: int = 200) -> None:
    body = json.dumps(payload, default=str).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Cache-Control", "no-store")
    _security_headers(handler)
    handler.end_headers()
    handler.wfile.write(body)


def _send_static(handler: BaseHTTPRequestHandler, name: str) -> None:
    if name not in ALLOWED_STATIC:
        handler.send_response(404)
        _security_headers(handler)
        handler.end_headers()
        return
    path = STATIC_DIR / name
    if not path.exists():
        handler.send_response(404)
        _security_headers(handler)
        handler.end_headers()
        return
    mime, _ = mimetypes.guess_type(str(path))
    body = path.read_bytes()
    handler.send_response(200)
    handler.send_header("Content-Type", mime or "application/octet-stream")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Cache-Control", "public, max-age=60")
    _security_headers(handler)
    handler.end_headers()
    handler.wfile.write(body)


# ---------- API endpoints ---------------------------------------------------

def _api_status() -> dict:
    state = read_json(STATE_FILE, default={})
    metrics = read_json(METRICS_FILE, default={})
    network = read_json(NETWORK_STATE_FILE, default={"state": "ONLINE"})
    started_at = state.get("started_at")
    return {
        "instance_id": state.get("instance_id", "SENTINEL-01"),
        "mode": state.get("mode", "ASSISTED"),
        "started_at": started_at,
        "uptime_seconds": metrics.get("uptime_seconds", 0),
        "overall": metrics.get("overall", "unknown"),
        "network_state": network.get("state", "ONLINE"),
        "tasks_completed": state.get("tasks_completed", 0),
        "tasks_failed":    state.get("tasks_failed", 0),
        "tasks_escalated": state.get("tasks_escalated", 0),
        "quarantined_components": state.get("quarantined_components", []),
        "watchdog": state.get("watchdog", {}),
        "now": iso_now(),
    }


def _api_tasks(limit: int = 50) -> dict:
    history = read_json(TASK_HISTORY_FILE, default={"tasks": []})
    tasks = list(reversed(history.get("tasks", [])))[:limit]
    return {"tasks": tasks, "count": len(tasks), "now": iso_now()}


def _api_metrics() -> dict:
    return read_json(METRICS_FILE, default={
        "overall": "unknown",
        "disk":   {"usage_pct": 0, "status": "unknown"},
        "memory": {"usage_pct": 0, "status": "unknown"},
        "network": {"status": "unknown"},
    })


def _api_alerts() -> dict:
    history = read_json(TASK_HISTORY_FILE, default={"tasks": []})
    state = read_json(STATE_FILE, default={})
    escalations = [
        t for t in history.get("tasks", [])
        if t.get("status") in ("escalated", "quarantined", "failed")
    ]
    escalations = list(reversed(escalations))[:50]
    return {
        "escalations": escalations,
        "quarantined_components": state.get("quarantined_components", []),
        "last_quarantine_at": state.get("last_quarantine_at"),
        "last_release_at":    state.get("last_release_at"),
    }


# ---------- handler ---------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    server_version = "SENTINEL-Dashboard/1.0"

    def log_message(self, format, *args):  # noqa: A003,N802
        LOG.info("%s - %s", self.address_string(), format % args)

    def do_GET(self):  # noqa: N802
        path = urlparse(self.path).path

        if path == "/" or path == "":
            return _send_static(self, "index.html")

        if path.startswith("/api/"):
            try:
                if path == "/api/status":   return _send_json(self, _api_status())
                if path == "/api/tasks":    return _send_json(self, _api_tasks())
                if path == "/api/metrics":  return _send_json(self, _api_metrics())
                if path == "/api/alerts":   return _send_json(self, _api_alerts())
                return _send_json(self, {"error": "unknown endpoint", "path": path}, status=404)
            except Exception as exc:
                LOG.error("api error %s: %s", path, exc)
                return _send_json(self, {"error": str(exc)}, status=500)

        # Static asset
        name = path.lstrip("/")
        return _send_static(self, name)

    def do_POST(self):  # noqa: N802
        # No write API on the dashboard. Reject explicitly.
        _send_json(self, {"error": "read-only"}, status=405)


def serve(host: str = BIND_HOST, port: int = BIND_PORT) -> None:
    if host not in ("127.0.0.1", "::1", "localhost"):
        # Defense in depth: refuse anything that isn't loopback (CT-17).
        raise ValueError(f"refusing to bind dashboard to non-loopback host: {host!r}")
    server = ThreadingHTTPServer((host, port), Handler)
    LOG.info("dashboard API listening on http://%s:%s", host, port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        LOG.info("dashboard API stopping")
    finally:
        server.server_close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="api-server")
    parser.add_argument("--host", default=BIND_HOST,
                        help="bind host (loopback only — non-loopback is rejected)")
    parser.add_argument("--port", type=int, default=BIND_PORT)
    args = parser.parse_args(argv)
    serve(args.host, args.port)
    return 0


if __name__ == "__main__":
    sys.exit(main())
