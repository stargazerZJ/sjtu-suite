---
name: sjtu-suite-dev
description: "How to develop and extend sjtu-suite (the SJTU automation suite in this repo): architecture, conventions, how to add a client or daemon, and how to verify changes. Use when the task is changing/adding code in sjtusuite/, daemons/, or the CLIs — not when the task is just using the suite's features (use the sjtu-suite skill for that)."
---

# sjtu-suite — developer guide

You are modifying this repo. Read this top-to-bottom before writing code; it is
short and encodes conventions the codebase already follows.

## Ground rules

1. **Never commit secrets.** `credentials.json`, `password.txt`,
   `data/` (cookies, job state, logs) are gitignored — keep it that way. In
   examples/docs, always use `{"mode": "env", "key": "..."}` secret blocks.
2. **Never print or log credentials, tokens, or cookie contents.** The
   `get_logger(...)` loggers are shared; when adding `self.logger.debug(...)`
   calls, log *identifiers* (job_id, venue_id), not payloads with tokens.
3. **Requests to SJTU services go through `requests.Session` instances owned
   by `OAuthClientBase` subclasses.** Do not create bare `requests.get(...)`
   calls for authenticated endpoints.
4. **No test suite exists.** Verification is: imports resolve,
   CLI `--help` runs, `python -c` snippets constructing objects work, and
   (when credentials exist) a real dry-run. See "Verification" below.

## Environment

- Python **3.12+** (`.python-version` = 3.12), managed with **uv**.
- Setup: `uv sync` then `uv pip install -e .` — this registers the console
  scripts. Run everything via `uv run <cmd>` from the repo root (the venv is
   `.venv/`).
- Key deps: `requests`, `flask`, `apscheduler`, `click`, `rich`,
  `pycryptodome` (AES/RSA for the sports API), `flask-cors`, `tzdata`.
- Package layout: `sjtusuite` and `daemons` are both packaged (see
  `[tool.hatch.build.targets.wheel]` in `pyproject.toml`);
  `sjtusuite/data/points.json` is force-included (GPS track data for the PE
  client).

## Repository map

```
sjtusuite/                  # installable library package
├── auth/
│   ├── oauth_base.py       # OAuthClientBase: Session + LWPCookieJar + UA
│   └── jac_login.py        # JACLogin: JAccount OAuth2 + captcha + 2FA
├── clients/                # one module per SJTU service
│   ├── door.py, pe.py, checkin.py, video.py, canvas.py,
│   ├── sports.py, questionnaire.py, mail.py
│   └── library/{base,advanced}.py
├── cli/                    # click+rich CLIs (pe, sports, video, mail)
├── core/                   # config (paths), credentials, logging
├── notifications/          # ntfy + mail notifiers + daemon wrapper
├── servers/base.py         # shared Flask helpers (get_client_ip)
└── data/points.json        # packaged data (PE GPS track)
daemons/                    # long-running Flask services
├── door_server.py          # sjtu-door   (port 5000)
├── checkin_server.py       # sjtu-checkin (port 5002)
├── sports_server.py        # shim -> daemons/sports/
└── sports/                 # the sports daemon, split by concern
    ├── main.py             # argparse entrypoint
    ├── app.py              # Flask routes (dashboard REST API)
    ├── daemon.py           # SportsReservationDaemon (scheduler container)
    ├── job_management.py   # CRUD + persistence for ReservationJob
    ├── booking.py          # run_job / warmup / noon execution
    ├── catalog.py          # venue/availability API for the dashboard
    ├── models.py           # ReservationJob dataclass (+ from/to_dict)
    ├── dashboard.py        # inline HTML template
    └── constants.py        # ports, paths, job types, timing constants
data/                       # runtime (gitignored): sessions/, job JSON, logs
research/                   # reverse-engineering notes (sports, questionnaire)
```

Runtime state convention: **everything mutable lives under `data/**`
(`get_data_dir()`)** — cookies in `data/sessions/*.cookies` (LWPCookieJar
format), daemon job state in `data/sports_reservation_jobs.json`, daemon logs
in `data/*.log`. Use `get_data_dir()` / `get_sessions_dir()` from
`sjtusuite.core.config` for new state, never relative CWD paths.

## Core architecture

### Auth (the pattern everything shares)

- `OAuthClientBase` (auth/oauth_base.py): owns a `requests.Session`, an
  `LWPCookieJar` persisted at `data/sessions/<session_file>`, and a default
  desktop-Chrome User-Agent. `set_user_agent("mobile")` switches to the
  TaskCenter-app UA (needed by PE).
- `JACLogin(OAuthClientBase)`: implements the full JAccount OAuth2 flow —
  login page parse, captcha fetch + solve (remote solver at
  geek.sjtu.edu.cn, fallback to manual input), 2FA, redirect-following,
  including HTML meta-refresh / JS-location / auto-submit form handoffs
  (`_resolve_login_transition`).
- **Critical constructor flag:** `JACLogin(..., allow_interactive=True|False)`.
  Daemons pass `allow_interactive=False` — when 2FA or manual captcha entry
  would be needed, JACLogin raises `TwoFactorAuthenticationRequired` /
  `CaptchaRequired` (both subclass `InteractiveAuthenticationRequired`)
  instead of blocking on `input()`. Any new non-interactive code path must
  do the same. CLIs keep interactive login.
- Service clients subclass `OAuthClientBase` and *delegate* JAccount login to
  a shared `JACLogin` instance (`self.jac_login`). The typical client login
  method: hit the service URL with `allow_redirects=False`; if 302 to
  jaccount.sjtu.edu.cn → `self.jac_login.login(redirect_url)` → re-visit the
  service URL → `self.save_session()`.

When adding a client, copy the `door.py` shape: `validate_session()` /
`login()` + typed public methods returning dataclasses or plain dicts, and a
`__main__` demo block guarded behind `get_test_jac_login()`
(`password.txt`, first line username, second line password — local testing
only, never committed).

### Sports client specifics

`sports.py` is the most complex client (~1100 lines): AES+RSA-encrypted
order payloads (constants `SPORTS_CLIENT_ID`, `SPORTS_PUBLIC_KEY`),
cookie export/clone for reservation racing, slot ranking, slider-captcha
handling, and both `target_date` and `cron` preview builders. The daemon
(daemons/sports/) is composed via mixins
(`SportsJobManagementMixin`, `SportsCatalogMixin`, `SportsBookingRunnerMixin`)
onto `SportsReservationDaemon`. Behavior constants (retry interval, burst
window, warmup time) live in `daemons/sports/constants.py` — tune there, not
inline.

### Daemons

All three daemons are Flask apps created via a `create_app(...)` factory
(so they're importable without side effects — required: the wheel imports
them for entry points). Long-running work uses APScheduler
(`BackgroundScheduler`) + daemon threads; shared state is guarded by
`threading.RLock`/`Lock` and exposed as JSON under `/status`-style routes.
Daemons authenticate non-interactively (see auth above) and emit
notifications through `DaemonNotificationClient`
(sjtusuite/notifications/daemon.py), which wraps ntfy (and optionally SMTP)
configured via the `notifications` block of `credentials.json`.

`daemons/sports_server.py` is a compatibility shim re-exporting from
`daemons/sports/`. If you move things, keep the shim or update
`pyproject.toml` `[project.scripts]`.

### Credentials

`sjtusuite.core.credentials.CredentialProvider` (singleton `credentials`)
loads `credentials.json` from the project root and resolves secret blocks:
`{"mode": "env", "key": "VAR"}` or `{"mode": "literal", "value": "..."}`.
Add new config keys as `@property`s on the provider (see
`ntfy_config` / `mail_config` for the dict-shape pattern) and document them in
`credentials.json.example` + README.

### CLI conventions

click groups/commands + rich `Console`, `Table`, `Panel`, `Prompt`. Each CLI
module has `create_client()` building the client from the `credentials`
singleton and a `main()` referenced from `pyproject.toml` scripts. New user
entry points should follow this and be registered there. Non-interactive
flags matter: agents can't answer rich Prompts — for any new interactive
feature, always provide full CLI flags / JSON API equivalents.

## Verification checklist

Run from repo root (adjust if not installed: `uv pip install -e .` first):

```bash
uv run python -c "import sjtusuite, daemons"                       # imports
uv run sjtu-pe --help && uv run sjtu-sports --help \
  && uv run sjtu-mail --help && uv run sjtu-video --help           # CLIs
uv run python -c "from daemons.sports.app import create_app"       # app factories
uv run sjtu-pe status                                              # real login (needs creds)
```

For library-only changes, `uv run python -c "from sjtusuite.clients import X"`
plus instantiating the client with dummy args (no network) is the minimum
bar. Prefer `--dry-run` flags (e.g. `sjtu-pe run --dry-run`,
`POST /api/jobs/<id>/run {"dry_run": true}` on the sports daemon) when
touching anything that mutates real reservations — a real reservation costs
money and needs a payment step.

Conventions to keep when you finish: update `README.md` (commands, config
keys, API examples) and `credentials.json.example` when you add user-facing
surface. Commit messages follow the existing `feat:`/`fix:`/`refactor:`
conventional style.
