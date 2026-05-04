# SENTINEL — ARCHITECTURE

A bootstrap-to-production system that turns a blank machine into an
autonomous digital employee. Five layers, one bootstrap, one dashboard.

```
┌──────────────────────────────────────────────────────────────────────────┐
│  Layer 5 — Command Center        layer5-dashboard/                       │
│    api_server.py (127.0.0.1:8501)  index.html  dashboard.js  style.css   │
├──────────────────────────────────────────────────────────────────────────┤
│  Layer 4 — Self-Healing          layer4-selfheal/                        │
│    watchdog.py (Type=notify, WatchdogSec=30)  quarantine.py              │
│    network_sentinel.py (state machine)  disk-janitor.sh                  │
│    credential_refresh.py                                                 │
├──────────────────────────────────────────────────────────────────────────┤
│  Layer 3 — Autonomy Ladder       layer3-autonomy/                        │
│    mode_controller.py  confidence_scorer.py  escalation_handler.py       │
├──────────────────────────────────────────────────────────────────────────┤
│  Layer 2 — Agent Core            layer2-agent/                           │
│    task_queue.py (orchestrator)  ai_engine.py  output_router.py          │
│    rate_limiter.py  templates/{report,alert}.md  gas/Code.gs             │
├──────────────────────────────────────────────────────────────────────────┤
│  Layer 1 — Foundation            layer1-foundation/                      │
│    bootstrap.sh (pre-flight + install)  harden.sh  health-monitor.sh     │
│    log-rotate.conf  scheduler.sh                                         │
└──────────────────────────────────────────────────────────────────────────┘
```

Cross-cutting:

```
utils/              shared by every layer
  common.py         constants, paths, atomic JSON, logging factory
  log_sanitizer.py  redacts API keys / tokens before they hit logs
  validator.py      task validation, config validation, API-key validation
config/             YAML — sentinel / tasks / alerts
data/               runtime state — state.json, metrics.json,
                    task-history.json, task-cache.json, network-state.json,
                    quarantine/, outputs/
services/           systemd unit files
```

## Process model

| systemd unit                | Layer | Long-running? | Purpose                          |
|-----------------------------|-------|---------------|----------------------------------|
| `sentinel-agent.service`    | 2     | yes           | Polls GAS, runs AI, routes output |
| `sentinel-watchdog.service` | 4     | yes (notify)  | Monitors targets, restarts/quarantines |
| `sentinel-dashboard.service`| 5     | yes           | HTTP server on `127.0.0.1:8501`  |
| `sentinel-health.timer`     | 1     | -             | Triggers `sentinel-health.service` every minute |
| `sentinel-health.service`   | 1     | oneshot       | `health-monitor.sh --once` → metrics.json |

The watchdog uses `Type=notify` and `WatchdogSec=30` so systemd is the
**meta-watchdog** — if the watchdog itself hangs, systemd kills and
restarts it (Gate-2 FIX 2 / CT-11).

## Data flow

```
   ┌─Google Sheets (tasks tab)
   │
   ▼  POST {action: getNextTask, secret}
   GAS web app  ──── claims row, marks "processing" ────►  task_queue.py
                                                              │
                                                  ┌───────────┼───────────┐
                                                  ▼           ▼           ▼
                                       validate_task     ai_engine     output_router
                                                              │           │
                                                              ▼           ▼
                                                          Anthropic /   file / slack /
                                                          OpenAI        email
                                                              │
                                                              ▼
                                                          submitResult to GAS
                                                              │
                                              (offline → cached in data/task-cache.json)
```

## State files (data/)

| File                  | Writer(s)                  | Reader(s)                |
|-----------------------|----------------------------|--------------------------|
| `state.json`          | task_queue, mode_controller, watchdog, quarantine | api_server, mode_controller |
| `metrics.json`        | health-monitor.sh          | watchdog, api_server     |
| `task-history.json`   | task_queue, escalation_handler | mode_controller, api_server |
| `task-cache.json`     | task_queue                 | task_queue (offline drain) |
| `network-state.json`  | network_sentinel           | watchdog, api_server     |
| `quarantine/<c>.json` | quarantine                 | quarantine, api_server   |

All writes go through `utils.common.write_json` which uses temp file +
`os.replace` for atomicity.

## Mode transitions (Layer 3)

```
        50+ done, err<5%, esc<10%
ASSISTED  ────────────────────────►  AUTONOMOUS
   ▲                                       │
   │                                       │  err>15% OR 3 consecutive
   │                                       │  fails OR human override
   └───────────────────────────────────────┘
```

`SHADOW` is a one-way starting state in v1.0 — the mode controller will
not flip *into* SHADOW automatically; only `--set shadow` does that.

## Self-healing (Layer 4)

For each target the watchdog runs:

```
  check() → unhealthy
     ↓
  fails += 1
     ↓
  fails ≥ 5  ──►  quarantine.add(component) → systemctl stop+mask
     ↓ no
  backoff = [5, 10, 20, 40, 60][fails-1]
     ↓
  repair() (e.g. systemctl restart, run disk-janitor)
     ↓
  sleep(backoff) → next cycle
```

Targets: `sentinel-agent`, `sentinel-dashboard`, `network`, `disk`,
`memory`, `api-keys`. Network and api-keys are observational targets —
their `repair()` triggers a sibling component (network_sentinel,
credential_refresh).

## Security hardening summary

| Concern              | Mitigation                                                |
|----------------------|-----------------------------------------------------------|
| `.env` exposure (CT-16) | `chmod 600` + `chown sentinel:sentinel` in bootstrap   |
| API keys in logs (CT-19)| `utils.log_sanitizer` runs in every logger Formatter   |
| Dashboard on LAN (CT-17)| api_server raises if host ≠ loopback                   |
| GAS endpoint replay (CT-18)| Shared secret in POST body, never URL params        |
| Bootstrap edge cases (CT-01..04)| 5 pre-flight checks gate every install         |
| Watchdog SPOF (CT-11) | systemd `Type=notify` + `WatchdogSec=30` watches it     |
| Disk fill at 100% (CT-12)| `disk-janitor.sh` triggered at 85% (well before full)|
| Retry storms (CT-13) | `offline_max_retries` + per-task `max_retries_per_task` caps |
| Queue flood (CT-07)  | `queue.max_depth` causes back-pressure in task_queue     |

## Where each Gate-2 finding lives

| Gate-2 fix | Where it landed                                                      |
|------------|----------------------------------------------------------------------|
| FIX 1 (pre-flight)        | `bootstrap.sh` `check_*` functions               |
| FIX 2 (watchdog SPOF)     | `services/sentinel-watchdog.service` (`WatchdogSec`) + `watchdog.py` `_sd_notify` |
| FIX 3 (cred security)     | `bootstrap.sh prompt_credentials` (`chmod 600`)  |
| FIX 4 (log scrub)         | `utils/log_sanitizer.py` + `common.get_logger`   |
| FIX 5 (validation)        | `utils/validator.py` used by task_queue + bootstrap |
| FIX 6 (GAS auth)          | `gas/Code.gs authenticate_()` + `task_queue.GasClient._post` |
| FIX 7 (dashboard sec)     | `api_server._security_headers` + loopback check  |
| FIX 8 (queue/retry caps)  | `config/sentinel.yaml queue.*` + `task_queue.run_loop` |
| FIX 9 (metrics rotation)  | `disk-janitor.sh` (rotates >50MB → gzip archive) |
