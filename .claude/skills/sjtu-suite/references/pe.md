# PE running records

Submit simulated running sessions to pe.sjtu.edu.cn (the SJTU physical
education requirement). Module: `sjtusuite/clients/pe.py`
(`PEClient`, `LocationType`). The client impersonates the TaskCenter mobile
app UA (`set_user_agent("mobile")` in the constructor).

## CLI

```bash
uv run sjtu-pe run               # default ~2km run, timestamped 30 min ago
uv run sjtu-pe run -d 3.0        # ~3 km
uv run sjtu-pe run -t "2026-09-18 08:00"   # specific end-time (ISO)
uv run sjtu-pe run --dry-run     # preview the payload, upload nothing
uv run sjtu-pe status            # check PE login status
```

**Always `--dry-run` first**; `run` uploads a real record to the PE system
(educational-use caveat applies — confirm with the user before a real
upload).

## Python API

```python
from sjtusuite import JACLogin
from sjtusuite.clients.pe import PEClient

pe = PEClient(JACLogin(username, password))
pe.simulate_running(run_time=None, n=15000)
```

- `run_time`: datetime the run "ended"; defaults to now.
- `n`: number of GPS points sampled from the packaged track
  (`sjtusuite/data/points.json`); CLI derives ~6500 points/km. Falls back to
  a synthetic 2-point track if the dataset is too short (you'll see a
  warning; the fallback distance is fixed ~1.73 km, not `-d`).
- Distance is estimated at ~15 km/h over the track duration — `-d` is
  approximate by design.
- `upload_result` retries once on identity errors by re-logging-in
  (`clear_pe_session` + fresh login), so stale `pe_client.cookies` normally
  self-heals.

Gotchas:
- PE auth goes through JAccount but the client must be constructed with the
  **mobile** UA (already handled in `PEClient.__init__` — don't override
  with `set_user_agent("chrome")`).
- Sessions live in `data/sessions/pe_client.cookies`.
- `get_uid` failure falls back to a dummy UID and the upload will then fail
  server-side — check the returned API response, not just the absence of
  exceptions.
