---
name: sjtu-suite
description: "Use and operate the SJTU automation suite in this repo: open dorm doors, simulate PE runs, reserve sports venues (CLI or dashboard daemon), download Canvas lecture videos, manage library seats, read/send university mail, collect questionnaire responses, and run the door/checkin/sports daemons. Use when the user wants to actually invoke these tools (CLIs, Python API, or daemon HTTP APIs). For modifying the codebase itself, use the sjtu-suite-dev skill."
---

# sjtu-suite — usage guide

A collection of automation utilities for SJTU (Shanghai Jiao Tong University)
services. Everything authenticates with the same JAccount credentials.

**Read order:** this file first, then the reference file for the component
you're about to touch — each has exact APIs, recipes, and gotchas.

## Topic index

| Task | Reference file | Entry point |
|------|----------------|-------------|
| Sports venue reservation (CLI + daemon) | [references/sports.md](references/sports.md) | `sjtu-sports`, `sjtu-sportsd` |
| PE running records | [references/pe.md](references/pe.md) | `sjtu-pe` |
| Lecture video download | [references/video-canvas.md](references/video-canvas.md) | `sjtu-video` |
| Library seats | [references/library.md](references/library.md) | Python API |
| Dorm door | [references/door.md](references/door.md) | `sjtu-door`, `DoorClient` |
| University mail | [references/mail.md](references/mail.md) | `sjtu-mail` |
| Questionnaires | [references/questionnaire.md](references/questionnaire.md) | Python API |
| Long-running daemons (REST APIs) | [references/daemons.md](references/daemons.md) | `sjtu-door`, `sjtu-checkin`, `sjtu-sportsd` |

## Environment setup (do this first)

```bash
cd <repo root>
uv sync && uv pip install -e .    # one-time; venv is .venv/
uv run sjtu-sports --help         # sanity check
```

Run everything with `uv run <command>` from the repo root. Console scripts:
`sjtu-pe`, `sjtu-sports`, `sjtu-video`, `sjtu-mail`, `sjtu-door`,
`sjtu-checkin`, `sjtu-sportsd`.

## Credentials

All tools read `credentials.json` at the repo root (copy
`credentials.json.example`). Passwords should be env-backed:

```json
{
  "username": "your_jaccount",
  "password": {"mode": "env", "key": "JACCOUNT_PASSWORD"},
  "room_id": "dorm_room_id"
}
```

- Before anything network-touching: check `credentials.json` exists and, if
  the password block is `{"mode": "env", ...}`, that the env var is set in
  the shell (export it in the same Bash call you run the tool in).
- Sessions persist in `data/sessions/*.cookies` — repeat runs reuse them and
  skip the captcha/2FA flow. If auth behaves oddly, deleting a client's
  `.cookies` file forces a fresh login (may require interactive 2FA/captcha
  once; daemons cannot do interactive auth — see below).

## Auth behavior that matters to agents

- First login may need a **captcha** (auto-solved by a remote solver, with
  manual fallback) or **2FA** — interactive prompts. CLIs can prompt; the
  daemons run with `allow_interactive=False` and fail fast with
  `TwoFactorAuthenticationRequired`/`CaptchaRequired` instead.
- If daemons hit that: run the corresponding CLI once in the terminal (or
  any interactive client) to refresh the saved session, then restart the
  daemon.
- Prefer `--dry-run` / dry-run API options before anything that books,
  uploads, or sends. Sports reservations create real (paid) orders.

## Fastest paths

```bash
# Sports: browse then book (CLI)
uv run sjtu-sports venues
uv run sjtu-sports availability <venue_id> --motion 乒乓球
uv run sjtu-sports reserve <venue_id> --motion 乒乓球 \
  --date 2026-09-20 --field 场地10 --time 19:00-20:00

# PE: dry-run first, then upload
uv run sjtu-pe run --dry-run && uv run sjtu-pe run

# Mail
uv run sjtu-mail list -n 20
uv run sjtu-mail read <uid>
uv run sjtu-mail send -t name@sjtu.edu.cn -s "Hi" -b "Body"

# Videos (interactive picker)
uv run sjtu-video

# Daemons
uv run sjtu-sportsd -p 5003      # dashboard: http://localhost:5003/
uv run sjtu-door -p 5000         # POST /open with token header
uv run sjtu-checkin -p 5002 --poll-url <source>
```

Python API (all under `sjtusuite`):

```python
from sjtusuite import JACLogin
from sjtusuite.clients import DoorClient, PEClient, VideoClient, SportsReservationClient
from sjtusuite.clients import CanvasClient, QuestionnaireClient, MailClient
from sjtusuite.clients.library import LibrarySeatClient

jac = JACLogin(username, password)   # from credentials.json
```

Each reference file shows the per-client construction and method calls.

## Notifications (optional)

Daemons push events via ntfy (and optionally SMTP) configured under the
`notifications` block of `credentials.json`. See README for the schema.
