# Long-running daemons

Three Flask services, all started with `uv run <cmd>` from the repo root, all
state under `data/`, all reading `credentials.json`, all pushing
notifications via the `notifications` block (ntfy, optional mail). Auth is
**non-interactive** — 2FA/captcha demands become clear errors, not prompts.
Fix by refreshing the session interactively once (run the matching CLI),
then restart the daemon.

## sjtu-door (door opening API) — port 5000

```bash
uv run sjtu-door -p 5000
```

Single endpoint:

```bash
curl -X POST http://localhost:5000/open -H "token: <door_access_token>"
# 200 {"status": "Door opened successfully"} | 401 | 500
```

- Token = `door_access_token` from `credentials.json` (falls back to a random
  UUID printed in logs — set it explicitly).
- The daemon refreshes the door session every 30 min; auth failures from an
  IP notify (rate-limited 1/min/IP). Log: `door_access.log` in repo root.
- Requires `room_id` in `credentials.json`.

## sjtu-checkin (course checkin poller) — port 5002

```bash
uv run sjtu-checkin -p 5002 --poll-url http://your-source/checkin-data
# or via credentials.json: checkin_poll_url / checkin_poll_interval
```

Polls a JSON source for check-in URLs (`courseCode` / `signHistoryId`
params) and auto-submits each through `CheckinClient` the moment it appears.
Routes: `/` (dashboard), `/status`, `/history`. Poll interval default 1 s.
There is no dry-run — it attempts real checkins by design; only run it when
the user wants auto checkin for their courses.

## sjtu-sportsd (sports reservation scheduler) — port 5003

See [sports.md](sports.md) for the full REST API, job schema, and gotchas.
Summary: `uv run sjtu-sportsd -p 5003` → dashboard at
`http://localhost:5003/`, jobs persisted in
`data/sports_reservation_jobs.json`, noon (12:00 Asia/Shanghai) booking
window, `POST /api/jobs/<id>/run {"dry_run": true}` for safe testing.

## Running them persistently

They are plain Flask dev servers (`app.run`) — no built-in daemonization.
Options: run under `tmux`/`nohup`, or a launchd/systemd unit wrapping
`uv run`. All bind `127.0.0.1` by default; `--host 0.0.0.0` exposes them
(sportsd and door accept tokens/none — only do this on trusted networks).

## Shared code

- `create_app(...)` factory pattern in each daemon module — importable
  without starting servers.
- APScheduler `BackgroundScheduler` (Asia/Shanghai) for periodic work.
- `DaemonNotificationClient` (sjtusuite/notifications/daemon.py) wraps ntfy
  for daemon events; best-effort, never raises.
- `sjtusuite/servers/base.py` provides `get_client_ip()`.
