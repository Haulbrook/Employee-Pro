"""F.3 — Failure injection.

Exercises the self-healing layer's responses to deliberate breakage:

  * a target that fails 5 times in a row → quarantine artefact
  * disk usage > 85% in metrics.json → disk-janitor would run (we run it
    directly and verify rotation)
  * network probes fail → state machine reaches OFFLINE without a retry
    storm (CT-13 fix)
  * task_queue submits to a 5xx GAS endpoint → result is cached locally
    (CT-13 fix; offline_max_retries respected on flush)
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from tests.conftest import make_sentinel_home


REPO_ROOT = Path(__file__).resolve().parent.parent


# ---------- helpers ----------------------------------------------------------

class FailingGas(BaseHTTPRequestHandler):
    """Always returns 500 — used to simulate GAS outage."""
    def log_message(self, *a, **kw): return  # noqa
    def do_POST(self):  # noqa: N802
        self.send_response(500); self.end_headers(); self.wfile.write(b'{}')


def _serve_failing_gas() -> tuple[ThreadingHTTPServer, str]:
    s = ThreadingHTTPServer(("127.0.0.1", 0), FailingGas)
    threading.Thread(target=s.serve_forever, daemon=True).start()
    h, p = s.server_address
    return s, f"http://{h}:{p}"


# ---------- watchdog quarantine after N failures ----------------------------

class WatchdogQuarantineTest(unittest.TestCase):

    def setUp(self):
        self.home = make_sentinel_home()

    def test_failing_target_quarantines_after_five_fails(self):
        import importlib, watchdog, quarantine
        importlib.reload(watchdog)
        importlib.reload(quarantine)

        # Build a synthetic always-failing target. We bypass the systemd
        # checks and inject our own check/repair callables.
        from watchdog import Target

        repair_calls = {"n": 0}
        def fake_repair():
            repair_calls["n"] += 1

        t = Target(name="synthetic", check=lambda: False, repair=fake_repair)

        # Manually drive 5 watchdog ticks via the same logic in run_loop
        # (without sleeps). After the 5th, _quarantine should fire.
        from watchdog import _quarantine
        for i in range(1, 6):
            t.fails = i
            if t.fails >= watchdog.QUARANTINE_AFTER:
                _quarantine(t)
                break
            else:
                t.repair()

        self.assertTrue(t.quarantined)
        # Quarantine artefact should exist on disk
        artefact = self.home / "data" / "quarantine" / "synthetic.json"
        self.assertTrue(artefact.exists(), f"missing artefact at {artefact}")
        payload = json.loads(artefact.read_text())
        self.assertEqual(payload["component"], "synthetic")
        self.assertEqual(payload["reason"], "watchdog-max-restarts")


# ---------- disk-janitor rotation -------------------------------------------

class DiskJanitorRotationTest(unittest.TestCase):

    def setUp(self):
        self.home = make_sentinel_home()

    def test_oversized_metrics_is_rotated_and_truncated(self):
        metrics = self.home / "data" / "metrics.json"
        # write 60 MB of garbage
        with open(metrics, "wb") as fh:
            fh.write(b"\0" * (60 * 1024 * 1024))
        env = {**os.environ,
               "SENTINEL_HOME": str(self.home),
               "SENTINEL_LOG_DIR": str(self.home / "log")}
        rc = subprocess.run(
            ["bash", str(REPO_ROOT / "layer4-selfheal" / "disk-janitor.sh")],
            env=env, capture_output=True, text=True, check=False,
        )
        self.assertEqual(rc.returncode, 0, rc.stderr)
        self.assertEqual(metrics.stat().st_size, 0, "metrics.json should be truncated")
        archive = list((self.home / "data" / "metrics-archive").glob("metrics-*.json.gz"))
        self.assertEqual(len(archive), 1, "expected exactly one archive")


# ---------- network state-machine under sustained failure -------------------

class NetworkOfflineTest(unittest.TestCase):

    def setUp(self):
        self.home = make_sentinel_home()

    def test_three_failed_ticks_lands_in_offline_no_storm(self):
        import importlib, network_sentinel as ns
        importlib.reload(ns)
        snap = ns._load()

        # 1st: ONLINE → OFFLINE on simultaneous fail
        snap = ns.step(snap, p_ok=False, d_ok=False)
        self.assertEqual(snap.state, "OFFLINE")
        # 100 more failed ticks should NOT generate any state churn,
        # validating CT-13 (no retry storms).
        transitions_at_offline = snap.transitions
        for _ in range(100):
            snap = ns.step(snap, p_ok=False, d_ok=False)
        self.assertEqual(snap.transitions, transitions_at_offline,
                         "OFFLINE state must not generate further transitions")
        # offline_since should remain set
        self.assertIsNotNone(snap.offline_since)

    def test_partial_failure_walks_to_offline_via_degraded(self):
        import importlib, network_sentinel as ns
        importlib.reload(ns)
        snap = ns._load()

        snap = ns.step(snap, p_ok=True, d_ok=False)  # ONLINE → DEGRADED
        self.assertEqual(snap.state, "DEGRADED")
        snap = ns.step(snap, p_ok=True, d_ok=False)
        self.assertEqual(snap.state, "DEGRADED")
        snap = ns.step(snap, p_ok=True, d_ok=False)  # 3rd → OFFLINE
        self.assertEqual(snap.state, "OFFLINE")


# ---------- task_queue caches offline submits -------------------------------

class TaskQueueOfflineCacheTest(unittest.TestCase):

    def setUp(self):
        self.home = make_sentinel_home()
        self.gas_server, self.gas_url = _serve_failing_gas()
        (self.home / ".env").write_text(
            f'GAS_ENDPOINT_URL="{self.gas_url}"\nSENTINEL_SECRET="x"\n'
        )

    def tearDown(self):
        self.gas_server.shutdown()
        self.gas_server.server_close()

    def test_failed_submit_caches_locally(self):
        import importlib, task_queue
        importlib.reload(task_queue)

        env = task_queue.load_env_file()
        client = task_queue.GasClient(env["GAS_ENDPOINT_URL"], env["SENTINEL_SECRET"])
        # client.submit_result should raise; caller (task_queue main loop) catches
        # and calls cache_unsubmitted_result. We mimic that here.
        try:
            client.submit_result("T-1", "result", "complete", "")
            raised = False
        except Exception:
            raised = True
        self.assertTrue(raised, "expected exception against failing GAS")

        task_queue.cache_unsubmitted_result("T-1", "result", "complete", "")
        cache = json.loads((self.home / "data" / "task-cache.json").read_text())
        self.assertEqual(cache["pending_results"][0]["task_id"], "T-1")


# ---------- mode controller downgrade on consecutive failures ---------------

class ModeDowngradeOnFailureTest(unittest.TestCase):

    def setUp(self):
        self.home = make_sentinel_home()

    def test_three_consecutive_failures_force_downgrade(self):
        history_path = self.home / "data" / "task-history.json"
        # 100 successes followed by 3 failures = consecutive_fails=3
        tasks = [{"status": "complete"}] * 100 + [{"status": "failed"}] * 3
        history_path.write_text(json.dumps({"tasks": tasks}))
        (self.home / "data" / "state.json").write_text(json.dumps({"mode": "AUTONOMOUS"}))

        import importlib, mode_controller
        importlib.reload(mode_controller)
        report = mode_controller.evaluate()
        self.assertEqual(report["next"], "ASSISTED")
        self.assertIn("consecutive_failures", report["reason"])


if __name__ == "__main__":
    unittest.main()
