# SENTINEL — TROUBLESHOOTING

Common symptoms and their fixes. If a symptom isn't listed, start with
`sudo journalctl -u sentinel-agent -n 200`.

## Bootstrap fails with "Insufficient disk space"

`bootstrap.sh` requires **10 GB free**. Free space and re-run:

```bash
df -h /
sudo apt clean
sudo journalctl --vacuum-size=200M
```

## Bootstrap fails with "Cannot reach 8.8.8.8"

ICMP may be blocked on your network. The pre-flight uses `ping`, so
unblock or run on a network where ICMP works. There is no flag to skip
this check — it exists for a reason.

## Bootstrap aborts: "Existing install detected"

Pick the right mode:

```bash
sudo ./bootstrap.sh --upgrade     # keeps state and credentials
sudo ./bootstrap.sh --reinstall   # wipes /opt/sentinel and starts over
```

## `sentinel-agent` keeps restarting

Look at the most recent crash:

```bash
sudo journalctl -u sentinel-agent -n 100 --no-pager
```

Common causes:

| Symptom in log                       | Cause                                     | Fix                                                   |
|--------------------------------------|-------------------------------------------|-------------------------------------------------------|
| `GAS rejected our secret`            | `SENTINEL_SECRET` mismatch                | Re-sync the secret in `/opt/sentinel/.env` and the GAS Script Properties; restart |
| `non-JSON response from GAS`         | GAS web app not deployed as **Anyone**    | Re-deploy with Access = Anyone, copy new URL          |
| `No AI provider configured`          | Both `ANTHROPIC_API_KEY` and `OPENAI_API_KEY` are empty | Edit `.env`, restart |
| `requests.exceptions.ReadTimeout`    | Slow upstream / API outage                | The agent will fall through to the offline cache; nothing to do |
| `validation: …`                       | Bad row in the Sheets queue              | Fix the row; SENTINEL marked it as failed             |

## Dashboard shows "API UNREACHABLE"

The static page loaded but it can't reach `/api/*`. Almost always the
api server has crashed:

```bash
sudo systemctl status sentinel-dashboard
sudo systemctl restart sentinel-dashboard
```

If it can't bind to port 8501, something else is using it:

```bash
sudo ss -lntp | grep 8501
```

## Disk usage keeps growing

`disk-janitor.sh` runs only when watchdog sees disk > 85%. To force a
manual run:

```bash
sudo /opt/sentinel/layer4-selfheal/disk-janitor.sh
```

If `metrics.json` is huge, it will be rotated to
`data/metrics-archive/` as gzip.

## Watchdog has quarantined a component

Inspect:

```bash
sudo /opt/sentinel/.venv/bin/python /opt/sentinel/layer4-selfheal/quarantine.py list
```

Each entry shows the metrics snapshot at the time of quarantine. After
fixing the root cause:

```bash
sudo /opt/sentinel/.venv/bin/python /opt/sentinel/layer4-selfheal/quarantine.py release <component>
```

## Mode keeps oscillating between ASSISTED and AUTONOMOUS

This means tasks are succeeding just enough to promote, then failing
just enough to downgrade. Inspect the recent history:

```bash
jq '.tasks[-30:]' /opt/sentinel/data/task-history.json
```

If error rate is genuinely on the boundary, raise the promotion bar:

```yaml
# config/sentinel.yaml
modes:
  thresholds:
    promote:
      max_error_rate: 0.02
```

Restart the agent for the new config to take effect.

## Network state stuck in OFFLINE

```bash
cat /opt/sentinel/data/network-state.json
```

If `ping_ok=true` and `dns_ok=true` for several cycles but the state is
still RECOVERING/OFFLINE, the recovery counter (`healthy_ticks`) is
working as designed. It needs `RECOVERY_TICKS=2` consecutive healthy
cycles before going ONLINE again. Wait one more cycle (15 s).

## "GAS rejected our secret (403)"

Caused by mismatch between `/opt/sentinel/.env` `SENTINEL_SECRET` and
the GAS Script Property of the same name. Update one or both, then:

```bash
sudo systemctl restart sentinel-agent
```

## Reset everything (dev-only)

```bash
sudo systemctl stop sentinel-agent sentinel-watchdog sentinel-dashboard
sudo bash /opt/sentinel/bootstrap.sh --reinstall
```

This wipes `/opt/sentinel` (including `data/`) and starts a clean
install.
