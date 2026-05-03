# SENTINEL GAS Backend (Phase B.1)

`Code.gs` is the Google Apps Script web app that backs the SENTINEL task queue.
This is a deployment artifact — it lives outside the systemd-managed install.

## Deploy

1. Create a Google Sheet. Add a tab named `tasks` with this header row:
   ```
   task_id | task_type | description | priority | status | assigned_to | created_at | completed_at | result | error
   ```
2. Extensions → Apps Script. Replace the default `Code.gs` with the file from
   this directory.
3. Project Settings → Script Properties:
   - `SENTINEL_SECRET` = a random string. Put the same value in
     `/opt/sentinel/.env` as `SENTINEL_SECRET=...`.
   - `TASKS_SHEET_NAME` = `tasks` (optional, defaults to `tasks`).
4. Deploy → New deployment → Web app → Execute as **Me**, Access **Anyone**.
5. Copy the deployed URL into `GAS_ENDPOINT_URL` in `/opt/sentinel/.env`.

## Authentication

Per Gate-2 FIX 6 / CT-18, the shared secret is sent in the POST **body**, not
in URL query params (which would be logged by Google). All POST handlers
return `{"http_code": 403, "error": "forbidden"}` if the secret is missing or
wrong.

## Actions

| action         | required body fields                          | returns                                   |
|----------------|-----------------------------------------------|-------------------------------------------|
| `getNextTask`  | `secret`, `instance_id`                       | `{task: {...} \| null}`                   |
| `submitResult` | `secret`, `task_id`, `result`, `status`       | `{ok: true, task_id, status}`             |
| `getStatus`    | `secret`                                      | `{counts, total, timestamp}`              |
| `updateTask`   | `secret`, `task_id`, `fields: {...}`          | `{ok: true, task_id, updated: [...]}`    |
| `enqueueTask`  | `secret`, `task_type`, `description`, ...     | `{ok: true, task_id}`                     |

`getNextTask` claims the row atomically (flips status to `processing` and
stamps `assigned_to`) before returning, so two SENTINEL instances cannot
double-process the same task.
