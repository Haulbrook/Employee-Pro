# SENTINEL — SETUP

This guide turns a blank machine into a running SENTINEL instance.

## 1. Pre-flight

A target machine must satisfy all five Gate-2 pre-flight checks (see
`bootstrap.sh`). The script aborts on any failure:

| Check          | Requirement                            |
|----------------|----------------------------------------|
| OS             | Ubuntu 22.04+ (primary) or macOS 12+   |
| Internet       | Reachable `8.8.8.8` + DNS for `google.com` |
| Disk           | ≥ 10 GB free at `/opt`                 |
| RAM            | ≥ 4 GB                                 |
| Idempotency    | No existing install (or use `--upgrade` / `--reinstall`) |

To check without installing:

```bash
sudo ./bootstrap.sh --check
```

## 2. Install

```bash
git clone https://github.com/Haulbrook/sentinel.git
cd sentinel
sudo ./bootstrap.sh
```

`bootstrap.sh` performs, in order:

1. Pre-flight checks
2. System packages: `python3 python3-pip python3-venv git jq curl ufw fail2ban logrotate`
3. Creates `sentinel` system user (Linux)
4. Creates `/opt/sentinel/` tree and `/var/log/sentinel/`
5. Deploys code into `/opt/sentinel/`
6. Creates Python venv and installs `requirements.txt`
7. Prompts for credentials (input is hidden) — see § 3
8. Writes `.env` with mode `0600`, owner `sentinel:sentinel` (Gate-2 FIX 3)
9. Runs `harden.sh` (UFW + fail2ban + SSH config)
10. Installs and enables systemd units
11. Starts `sentinel-agent`, `sentinel-watchdog`, `sentinel-dashboard`,
    `sentinel-health.timer`
12. Runs an initial health check
13. Writes `/var/sentinel/.installed` lock file
14. Prints `SENTINEL ONLINE` + dashboard URL

Estimated total time: 10–15 minutes on decent internet.

## 3. Credentials

The bootstrap prompts for these. Leave any blank to skip.

| Variable             | Required | Notes                                   |
|----------------------|----------|-----------------------------------------|
| `ANTHROPIC_API_KEY`  | one of   | Anthropic Claude API key                |
| `OPENAI_API_KEY`     | one of   | OpenAI API key                          |
| `GAS_ENDPOINT_URL`   | yes      | Google Apps Script web-app URL          |
| `SENTINEL_SECRET`    | yes      | Shared secret for the GAS endpoint      |
| `SLACK_WEBHOOK_URL`  | optional | Incoming webhook for the Slack channel  |
| `SMTP_HOST` … `_TO`  | optional | Email channel                           |

Generate `SENTINEL_SECRET` with:

```bash
python3 -c 'import secrets; print(secrets.token_urlsafe(32))'
```

The same value goes into the GAS Script Properties (see § 4).

## 4. Google Apps Script backend

1. Create a Google Sheet with a tab named `tasks` and the 10-column header
   row (see `layer2-agent/gas/README.md`).
2. Extensions → Apps Script. Replace the default `Code.gs` with the file
   in `layer2-agent/gas/Code.gs`.
3. Project Settings → Script Properties:
   - `SENTINEL_SECRET` = same value as `/opt/sentinel/.env`
   - `TASKS_SHEET_NAME` = `tasks` (optional)
4. Deploy → New deployment → **Web app**, Execute as **Me**, Access **Anyone**.
5. Copy the URL into `GAS_ENDPOINT_URL` in `/opt/sentinel/.env`.
6. Restart the agent: `sudo systemctl restart sentinel-agent`.

## 5. Reach the dashboard

The dashboard binds to `127.0.0.1:8501`. From the same machine:

```
http://127.0.0.1:8501
```

To view from another machine, use SSH port-forwarding:

```bash
ssh -L 8501:127.0.0.1:8501 sentinel-host
# then open http://localhost:8501 on your laptop
```

This is by design (CT-17 fix). The dashboard is never exposed to the LAN.

## 6. macOS notes

`bootstrap.sh` runs in a reduced mode on macOS:

- No `sentinel` user; runs as the invoking user.
- No UFW / fail2ban / sysctl hardening (`harden.sh` exits early).
- No systemd. Use `launchctl` plists if you want services to start at boot,
  or run the components manually under a screen/tmux session.

Linux is the recommended primary target.

## 7. Verifying the install

```bash
# all four services should be active:
systemctl status sentinel-agent sentinel-watchdog sentinel-dashboard sentinel-health.timer

# dashboard reachable on loopback only:
curl -fsS http://127.0.0.1:8501/api/status | jq

# logs:
sudo tail -f /var/log/sentinel/*.log
```
