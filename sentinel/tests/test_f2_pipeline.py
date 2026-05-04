"""F.2 — Task pipeline test.

End-to-end exercise of the agent core:

   stub-GAS  ──getNextTask──►  task_queue.GasClient  ──►  validator
                                                            │
                              stubbed ai_engine.process_task ◄──┘
                                                            │
                                                   output_router (file channel)
                                                            │
                              stub-GAS  ◄──submitResult──── │
                                                            ▼
                                              data/task-history.json appended
                                              data/state.json counters bumped

No network, no real Anthropic / OpenAI calls.
"""

from __future__ import annotations

import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

from tests.conftest import make_sentinel_home


# ---------- stub GAS endpoint ------------------------------------------------

class StubGasState:
    """Server-thread-shared state for the stub GAS endpoint."""

    def __init__(self):
        self.lock = threading.Lock()
        self.queued: list[dict] = []
        self.submitted: list[dict] = []
        self.expected_secret = "test-secret"


def _make_stub_gas_handler(state: StubGasState):

    class StubGasHandler(BaseHTTPRequestHandler):
        def log_message(self, *args, **_kw):  # silence
            return

        def _send(self, code: int, body: dict):
            data = json.dumps(body).encode()
            self.send_response(200)  # GAS always returns 200; logical code in body
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_POST(self):  # noqa: N802
            length = int(self.headers.get("Content-Length") or 0)
            payload = json.loads(self.rfile.read(length) or b"{}")

            if payload.get("secret") != state.expected_secret:
                return self._send(403, {"http_code": 403, "error": "forbidden"})

            with state.lock:
                action = payload.get("action")
                if action == "getNextTask":
                    if state.queued:
                        task = state.queued.pop(0)
                        return self._send(200, {"http_code": 200, "task": task})
                    return self._send(200, {"http_code": 200, "task": None})

                if action == "submitResult":
                    state.submitted.append({
                        "task_id": payload["task_id"],
                        "status": payload.get("status"),
                        "result": payload.get("result"),
                        "error": payload.get("error"),
                    })
                    return self._send(200, {"http_code": 200, "ok": True})

                if action == "getStatus":
                    return self._send(200, {"http_code": 200,
                                            "counts": {"pending": len(state.queued)},
                                            "total": len(state.queued)})
            return self._send(400, {"http_code": 400, "error": "unknown"})

    return StubGasHandler


def _serve_stub_gas(state: StubGasState) -> tuple[ThreadingHTTPServer, str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _make_stub_gas_handler(state))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    host, port = server.server_address
    return server, f"http://{host}:{port}"


# ---------- tests ------------------------------------------------------------

class PipelineTest(unittest.TestCase):

    def setUp(self):
        self.home = make_sentinel_home()
        self.gas_state = StubGasState()
        self.gas_server, self.gas_url = _serve_stub_gas(self.gas_state)

        # write a synthetic .env so GasClient picks up our stub URL + secret
        (self.home / ".env").write_text(
            f'GAS_ENDPOINT_URL="{self.gas_url}"\n'
            f'SENTINEL_SECRET="{self.gas_state.expected_secret}"\n'
            f'SENTINEL_INSTANCE_ID="SENTINEL-TEST"\n',
            encoding="utf-8",
        )

    def tearDown(self):
        self.gas_server.shutdown()
        self.gas_server.server_close()

    def _enqueue(self, task_id="T-001", task_type="generate", desc="Write a haiku"):
        with self.gas_state.lock:
            self.gas_state.queued.append({
                "task_id": task_id,
                "task_type": task_type,
                "description": desc,
                "priority": 3,
                "status": "pending",
            })

    def test_one_task_complete_round_trip(self):
        self._enqueue()

        # Reload task_queue with our test SENTINEL_HOME (fresh module state)
        import importlib, task_queue, ai_engine, output_router
        importlib.reload(task_queue)
        importlib.reload(ai_engine)
        importlib.reload(output_router)

        # Stub the AI provider call so we don't touch the network.
        def fake_process_task(task):
            from ai_engine import AIResult
            return AIResult(
                text=f"[stub-output for {task.task_id}]",
                provider="stub", model="stub-1",
                tokens_in=10, tokens_out=12,
            )
        ai_engine.process_task = fake_process_task  # type: ignore

        # Pull and process exactly one task by re-implementing one cycle
        # (avoids spinning up the full run_loop with sleeps).
        cfg = task_queue.load_config()
        env = task_queue.load_env_file()
        client = task_queue.GasClient(env["GAS_ENDPOINT_URL"], env["SENTINEL_SECRET"])
        task_dict = client.get_next_task("SENTINEL-TEST")
        self.assertIsNotNone(task_dict)

        from utils.validator import validate_task
        task = validate_task(task_dict)
        status, text, error = task_queue.process_task(
            task, mode="ASSISTED", ai_module=ai_engine, output_module=output_router
        )

        client.submit_result(task.task_id, text, status, error)
        task_queue.append_history({
            "task_id": task.task_id, "task_type": task.task_type,
            "status": status, "attempts": 1,
            "completed_at": task_queue.iso_now(),
        })
        task_queue.update_state(tasks_completed=1)

        # Verify outputs
        self.assertEqual(status, "complete")
        self.assertIn("stub-output", text)
        # File channel wrote a markdown file
        out = list((self.home / "data" / "outputs").glob("T-001*.md"))
        self.assertEqual(len(out), 1, "expected exactly one output file")
        # GAS got the submit
        self.assertEqual(len(self.gas_state.submitted), 1)
        self.assertEqual(self.gas_state.submitted[0]["status"], "complete")
        # State + history persisted
        history = json.loads((self.home / "data" / "task-history.json").read_text())
        self.assertEqual(history["tasks"][-1]["task_id"], "T-001")
        state = json.loads((self.home / "data" / "state.json").read_text())
        self.assertEqual(state["tasks_completed"], 1)

    def test_validation_failure_marks_task_failed(self):
        self.gas_state.queued.append({
            "task_id": "T-BAD",
            "task_type": "not-a-real-type",
            "description": "x",
            "priority": 3,
        })
        import importlib, task_queue
        importlib.reload(task_queue)

        env = task_queue.load_env_file()
        client = task_queue.GasClient(env["GAS_ENDPOINT_URL"], env["SENTINEL_SECRET"])
        task_dict = client.get_next_task("SENTINEL-TEST")
        from utils.validator import validate_task, ValidationError
        with self.assertRaises(ValidationError):
            validate_task(task_dict)
        # Mimic what task_queue does on validation failure
        client.submit_result("T-BAD", result="", status="failed", error="validation")
        self.assertEqual(self.gas_state.submitted[0]["status"], "failed")
        self.assertEqual(self.gas_state.submitted[0]["error"], "validation")

    def test_offline_caches_result(self):
        """When GAS is unreachable, results are queued in task-cache.json."""
        # Kill the stub server before submitting
        self.gas_server.shutdown()

        import importlib, task_queue
        importlib.reload(task_queue)

        task_queue.cache_unsubmitted_result("T-OFF", "result text", "complete", "")
        cache = json.loads((self.home / "data" / "task-cache.json").read_text())
        self.assertEqual(len(cache["pending_results"]), 1)
        self.assertEqual(cache["pending_results"][0]["task_id"], "T-OFF")


if __name__ == "__main__":
    unittest.main()
