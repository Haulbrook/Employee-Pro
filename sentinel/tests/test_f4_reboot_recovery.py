"""F.4 — Reboot recovery.

A real reboot test belongs in the manual VM fixture (see
``tests/fixtures/vm-bootstrap.sh``). The behaviours we can verify in CI:

  * All durable state files (state.json, task-history.json, task-cache.json,
    network-state.json, quarantine/*.json) survive a process restart and
    are correctly re-read by their owning module.
  * Cached offline results are flushed once the GAS endpoint comes back.
  * systemd unit files declare the correct restart policy and ordering so
    that the host's boot sequence will revive SENTINEL without manual help.
"""

from __future__ import annotations

import json
import re
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from tests.conftest import make_sentinel_home


REPO_ROOT = Path(__file__).resolve().parent.parent


# ---------- state survives a restart ---------------------------------------

class StateFilesSurviveRestartTest(unittest.TestCase):

    def setUp(self):
        self.home = make_sentinel_home()

    def test_state_and_history_round_trip(self):
        # Write state via task_queue helpers, "restart" by reloading the
        # module, verify everything is still there.
        import importlib, task_queue
        importlib.reload(task_queue)

        task_queue.update_state(
            mode="AUTONOMOUS",
            tasks_completed=42,
            tasks_failed=3,
            quarantined_components=["disk"],
        )
        task_queue.append_history({
            "task_id": "T-100",
            "task_type": "generate",
            "status": "complete",
        })

        # "Reboot" — drop module references and reload.
        for mod in ("task_queue",):
            if mod in dir():
                del task_queue
        importlib.invalidate_caches()
        import task_queue as task_queue2  # noqa
        importlib.reload(task_queue2)

        from utils.common import STATE_FILE, TASK_HISTORY_FILE, read_json
        state = read_json(STATE_FILE, default={})
        history = read_json(TASK_HISTORY_FILE, default={})

        self.assertEqual(state["mode"], "AUTONOMOUS")
        self.assertEqual(state["tasks_completed"], 42)
        self.assertEqual(state["tasks_failed"], 3)
        self.assertEqual(state["quarantined_components"], ["disk"])
        self.assertEqual(history["tasks"][-1]["task_id"], "T-100")


class QuarantineArtefactSurvivesTest(unittest.TestCase):

    def setUp(self):
        self.home = make_sentinel_home()

    def test_quarantine_artefact_persists_and_is_listed_after_reload(self):
        import importlib, quarantine
        importlib.reload(quarantine)

        quarantine.quarantine("disk", reason="usage_pct=92")

        # "Reboot" — reload and confirm list_quarantined finds it
        importlib.reload(quarantine)
        listed = quarantine.list_quarantined()
        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0]["component"], "disk")

    def test_release_after_restart_clears_state(self):
        import importlib, quarantine
        importlib.reload(quarantine)
        quarantine.quarantine("memory", reason="usage_pct=99")

        importlib.reload(quarantine)
        ok = quarantine.release("memory")
        self.assertTrue(ok)
        self.assertEqual(quarantine.list_quarantined(), [])


# ---------- offline cache drains when GAS returns --------------------------

class OfflineCacheDrainsOnReconnect(unittest.TestCase):

    def setUp(self):
        self.home = make_sentinel_home()
        self.received: list[dict] = []
        self._build_server()
        (self.home / ".env").write_text(
            f'GAS_ENDPOINT_URL="{self.url}"\nSENTINEL_SECRET="t"\n'
        )

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def _build_server(self):
        received = self.received

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a, **kw): return  # noqa
            def do_POST(self):  # noqa: N802
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}")
                received.append(body)
                resp = json.dumps({"http_code": 200, "ok": True}).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(resp)))
                self.end_headers()
                self.wfile.write(resp)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        h, p = self.server.server_address
        self.url = f"http://{h}:{p}"

    def test_drains_pending_results(self):
        # Pretend the agent crashed mid-cycle with two unsubmitted results.
        cache = self.home / "data" / "task-cache.json"
        cache.write_text(json.dumps({
            "pending_results": [
                {"task_id": "T-A", "result": "rA", "status": "complete", "error": "", "queued_at": "x", "attempts": 0},
                {"task_id": "T-B", "result": "rB", "status": "complete", "error": "", "queued_at": "x", "attempts": 0},
            ],
            "saved_tasks": [],
        }))

        # "Reboot" — fresh task_queue module
        import importlib, task_queue
        importlib.reload(task_queue)

        env = task_queue.load_env_file()
        client = task_queue.GasClient(env["GAS_ENDPOINT_URL"], env["SENTINEL_SECRET"])
        flushed = task_queue.flush_cached_results(client, max_attempts=10)
        self.assertEqual(flushed, 2)

        # cache should now be empty
        cache_after = json.loads(cache.read_text())
        self.assertEqual(cache_after["pending_results"], [])
        # GAS got both submits
        ids = sorted(b["task_id"] for b in self.received if b.get("action") == "submitResult")
        self.assertEqual(ids, ["T-A", "T-B"])


# ---------- systemd unit recovery semantics --------------------------------

class SystemdUnitRecoveryTest(unittest.TestCase):
    """Static-analysis on the unit files. systemd is what reboots the host
    services; we just verify it's been told the right things."""

    UNITS = [
        REPO_ROOT / "services" / "sentinel-agent.service",
        REPO_ROOT / "services" / "sentinel-watchdog.service",
        REPO_ROOT / "services" / "sentinel-dashboard.service",
    ]

    def _section(self, text: str, name: str) -> str:
        m = re.search(rf"\[{name}\](.*?)(?=^\[|\Z)", text, re.S | re.M)
        return m.group(1) if m else ""

    def test_each_long_running_unit_restarts_always(self):
        for unit in self.UNITS:
            text = unit.read_text()
            service = self._section(text, "Service")
            self.assertRegex(service, r"(?m)^Restart=always",
                             f"{unit.name}: missing Restart=always")
            self.assertRegex(service, r"(?m)^RestartSec=",
                             f"{unit.name}: missing RestartSec=")
            install = self._section(text, "Install")
            self.assertRegex(install, r"(?m)^WantedBy=multi-user\.target",
                             f"{unit.name}: should be enabled at boot")

    def test_agent_and_watchdog_wait_for_network(self):
        for name in ("sentinel-agent.service", "sentinel-watchdog.service"):
            unit = REPO_ROOT / "services" / name
            text = unit.read_text()
            self.assertIn("network-online.target", text,
                          f"{name}: must order after network-online.target")

    def test_watchdog_meta_supervised_by_systemd(self):
        text = (REPO_ROOT / "services" / "sentinel-watchdog.service").read_text()
        self.assertIn("Type=notify", text)
        self.assertIn("WatchdogSec=30", text,
                      "Gate-2 FIX 2 / CT-11 requires WatchdogSec on the watchdog itself")

    def test_dashboard_loopback_only(self):
        # The dashboard.service does not specify a host (api_server.py defaults
        # to 127.0.0.1). Belt-and-braces: api_server.serve() rejects non-loopback.
        import importlib, api_server
        importlib.reload(api_server)
        with self.assertRaises(ValueError):
            api_server.serve(host="0.0.0.0", port=18599)


if __name__ == "__main__":
    unittest.main()
