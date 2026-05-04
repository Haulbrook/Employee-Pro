# SENTINEL — RUNBOOK

Day-to-day operational procedures.

## Daily checks (≤ 2 minutes)

1. Open `http://127.0.0.1:8501` (or via SSH tunnel).
2. Confirm the status dot is **green** (`overall = ok`).
3. Confirm `Quarantined: none`.
4. Skim the Alert Log for new escalations.
5. Skim the Task History — no unexpected `failed` runs.

If any of those are off, jump to the Incident section below.

## Weekly checks (≤ 10 minutes)

1. Performance Review on the dashboard:
   - Completion rate ≥ 95%
   - Error rate ≤ 5%
   - Escalation rate ≤ 10%
2. `du -sh /var/log/sentinel /opt/sentinel/data` — confirm growth is
   bounded.
3. Run `credential_refresh.py` to revalidate API keys:

   ```bash
   sudo /opt/sentinel/.venv/bin/python \
        /opt/sentinel/layer4-selfheal/credential_refresh.py
   ```

## Updating SENTINEL

```bash
cd ~/sentinel        # source checkout
git pull
sudo ./bootstrap.sh --upgrade
```

`--upgrade` keeps `/opt/sentinel/.env`, `data/`, and runtime state
intact. It re-runs `harden.sh` and re-installs systemd units.

## Adding a task by hand

Open the Google Sheet's `tasks` tab, add a row:

| task_id | task_type | description | priority | status |
|---------|-----------|-------------|----------|--------|
| T-100   | generate  | Write a Q3 summary of jobsite hours | 3 | pending |

Within 60 s the agent claims it (status flips to `processing`) and
within ~ a minute it is `complete` with `result` populated.

Or POST to the GAS endpoint:

```bash
curl -fsS -X POST "$GAS_ENDPOINT_URL" \
  -H 'Content-Type: application/json' \
  -d '{"action":"enqueueTask","secret":"…","task_type":"generate","description":"…","priority":3}'
```

## Switching modes manually

```bash
# Force AUTONOMOUS (skip the criteria check)
sudo /opt/sentinel/.venv/bin/python \
     /opt/sentinel/layer3-autonomy/mode_controller.py --set autonomous

# Roll back to ASSISTED
sudo … mode_controller.py --set assisted

# Quiet mode — agent will log incoming tasks but not execute them
sudo … mode_controller.py --set shadow
```

## Releasing a quarantined component

```bash
sudo /opt/sentinel/.venv/bin/python /opt/sentinel/layer4-selfheal/quarantine.py list
# fix root cause
sudo /opt/sentinel/.venv/bin/python /opt/sentinel/layer4-selfheal/quarantine.py release <component>
```

## Rotating credentials

1. Generate the new key with the provider.
2. Edit `/opt/sentinel/.env` (preserve `chmod 600`).
3. Restart the agent:

   ```bash
   sudo systemctl restart sentinel-agent
   ```

4. Verify:

   ```bash
   sudo /opt/sentinel/.venv/bin/python /opt/sentinel/layer4-selfheal/credential_refresh.py
   ```

## Backups

Daily configuration snapshot (cron):

```bash
sudo tar czf "/var/backups/sentinel-$(date -u +%F).tgz" \
    /opt/sentinel/config /opt/sentinel/.env /opt/sentinel/data
```

`.env` should *only* be backed up to encrypted-at-rest storage.

## Incident: agent is offline

1. `systemctl status sentinel-agent`
2. If failed: `journalctl -u sentinel-agent -n 200 --no-pager`
3. Cross-reference with `docs/TROUBLESHOOTING.md` symptom table.
4. Restart: `systemctl restart sentinel-agent`.
5. If it crash-loops > 5 times the watchdog will quarantine it. Inspect
   `data/quarantine/sentinel-agent.json` and follow the **release**
   procedure above.

## Incident: dashboard is unreachable

1. `systemctl status sentinel-dashboard`
2. `ss -lntp | grep 8501`
3. `journalctl -u sentinel-dashboard -n 50`
4. Restart: `systemctl restart sentinel-dashboard`.

## Incident: API provider outage

The agent will queue work locally and retry up to
`queue.offline_max_retries` times (default 10). After that, the cached
result is dropped and the row in the Sheets queue stays in the most
recent status it was given.

To clear the cache manually if the outage is over and you want to
re-pull from GAS:

```bash
sudo systemctl stop sentinel-agent
sudo rm -f /opt/sentinel/data/task-cache.json
sudo systemctl start sentinel-agent
```

## Decommissioning

```bash
sudo systemctl stop sentinel-agent sentinel-watchdog sentinel-dashboard
sudo systemctl disable sentinel-agent sentinel-watchdog sentinel-dashboard
sudo systemctl disable sentinel-health.timer
sudo rm /etc/systemd/system/sentinel-*.service /etc/systemd/system/sentinel-*.timer
sudo systemctl daemon-reload
sudo userdel -r sentinel
sudo rm -rf /opt/sentinel /var/log/sentinel /var/sentinel
```
