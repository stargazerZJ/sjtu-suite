# Sports venue reservation

Two ways to use: the `sjtu-sports` CLI (one-shot browsing/booking) and the
`sjtu-sportsd` daemon (scheduled jobs with a REST dashboard). Both talk to
sports.sjtu.edu.cn via `SportsReservationClient`. Timezone everywhere is
Asia/Shanghai; reservations open at **noon (12:00)** for a target date.

Module: `sjtusuite/clients/sports.py`. Key exports:
`SportsReservationClient`, `CredentialsSportsAuthProvider`,
`SportsAPIError`, dataclasses `MotionType` / `VenueSummary` / `VenueDetail` /
`DateOption` / `FieldSlot` / `ReservationPreview`.

## CLI

```bash
uv run sjtu-sports                                # interactive picker
uv run sjtu-sports venues                         # all venues (all pages)
uv run sjtu-sports venues --page-num 2            # one page
uv run sjtu-sports availability <venue_id> --motion 乒乓球
uv run sjtu-sports reserve <venue_id> --motion 乒乓球 \
  --date 2026-09-20 --field 场地10 --time 19:00-20:00            # preview only
uv run sjtu-sports reserve ... --submit                          # create real order
uv run sjtu-sports reserve ... --submit --create-payment         # also get payment URL
```

- `reserve` without `--submit` is a **dry run** — it renders the preview
  (price, selected spaces) and fetches terms. Only add `--submit` when the
  user explicitly wants to book: orders are real and need payment.
- `--field` and `--time` are repeatable for multi-slot orders.
- If the API answers `code 1002`, a slider captcha is required: pass
  `--captcha-verification <string>`. This is rare in CLI flows but the flag
  exists; the exact verification string comes from
  `client.build_captcha_verification(...)` after solving
  `client.get_slider_captcha()`.

## Python API

```python
from sjtusuite.clients.sports import CredentialsSportsAuthProvider

provider = CredentialsSportsAuthProvider(username, password, allow_interactive=True)
client = provider.create_client()   # logs in and returns a ready client

# Browse
venues, total = client.list_venues(venue_name="南区")   # or list_all_venues()
detail = client.get_venue_detail(venues[0].venue_id)    # .motion_types, .venue_mobile
avail = client.list_availability(venue_id, "乒乓球")    # date-level rows

# Inspect slots for one date
motion_type = client.resolve_motion_type(venue_id, "乒乓球")  # name or id
dates = client.list_date_options(venue_id, motion_type.id)
slots = client.list_available_slots(venue_id, motion_type.id,
                                    date="2026-09-20", date_id=dates[0].date_id)

# Preview -> confirm -> pay
preview = client.preview_personal_order(
    venue_id=venue_id, motion="乒乓球", date="2026-09-20",
    field_names=["场地10"], time_slots=["19:00-20:00"],
)
resp = client.confirm_personal_order(preview["confirm_order_payload"])  # {"code":0,"data":<order_id>}
payment = client.create_payment(resp["data"])        # optional: payment_url
```

Availability errors: `SportsAPIError` when the date isn't exposed for the
venue, slot not selectable, etc. The `FieldSlot.is_selectable()` flag and
statuses in `UNAVAILABLE_STATUSES = {-3,-2,-1}` mark taken slots.
`client.choose_slots` / `rank_slot_candidates` pick best slots by policy
(`SLOT_SELECTION_MODE_ALL_REQUIRED` vs `FIRST_AVAILABLE`).

## Daemon (sjtu-sportsd)

Start: `uv run sjtu-sportsd -p 5003` (binds 127.0.0.1; `--host 0.0.0.0` to
expose). Dashboard at `http://localhost:5003/`. State persists in
`data/sports_reservation_jobs.json`; log at
`data/sports_reservation_daemon.log`.

REST API (JSON):

| Method + path | Purpose |
|---|---|
| `GET /api/status` | daemon status, all jobs, recent history |
| `GET /api/catalog/venues?search=` | venue search |
| `GET /api/catalog/venues/<venue_id>` | venue detail + motion types |
| `GET /api/catalog/venues/<venue_id>/availability?motion=乒乓球` | availability |
| `GET/POST /api/jobs` | list / create jobs |
| `PATCH/DELETE /api/jobs/<job_id>` | update / delete |
| `POST /api/jobs/<job_id>/run` | manual trigger; body `{"dry_run": true}` |

Minimal job payload:

```json
{
  "venue_id": "9775406a2ee4400082e59a2f4a6b7c4f",
  "venue_name": "南区体育中心",
  "motion": "乒乓球",
  "job_type": "target_date",
  "target_date": "2026-09-25",
  "run_on_date": "2026-09-24",
  "time_slots": ["19:00-20:00"]
}
```

- `job_type` `"target_date"`: fires at 12:00 on `run_on_date` (defaults to
  first upcoming noon) and books for `target_date`. Sub-second retry burst
  around noon (`retry_window_seconds` 180, `retry_interval_seconds` 0.2).
- `job_type` `"cron"`: scans every `cron_interval_minutes` (default 10) for
  free slots within `window_start_days`..`window_end_days` (0..7) — a
  cancellation watcher. Books only slots at least `redeem_deadline_hours`
  (default 2) before start.
- `slot_selection_mode`: `"all_required"` (default; all requested slots must
  be free) or `"first_available"`.
- `preferred_fields`: list of field names to prefer when picking.
- Always create jobs with `dry_run` manual run first:
  `POST /api/jobs/<id>/run {"dry_run": true}` to validate before noon.

The daemon runs non-interactive auth (`allow_interactive=False`). If JAccount
demands 2FA/captcha it errors clearly — fix by running an interactive client
once (e.g. `uv run sjtu-sports venues`) to refresh
`data/sessions/sports_client.cookies`, then restart the daemon.

Gotchas:
- The client encrypts order payloads (AES+RSA with constants in the module)
  — never hand-roll the confirm payload; use `preview_personal_order`.
- `credentials.username`/`password` come from `credentials.json` (env var
  must be exported).
- Deep reverse-engineering notes: `research/sjtu-sports-reservation-notes.md`.
