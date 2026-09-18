# Library seat reservation

Reserve/manage library seats via the seat system (ic-web API behind
seat.lib.sjtu.edu.cn-style endpoints). Python API only — no CLI. Modules:
`sjtusuite/clients/library/base.py` (`LibrarySeatClient`), `advanced.py`
(`LibrarySeatAdvancedClient`).

## Base client

```python
from sjtusuite import JACLogin
from sjtusuite.clients.library import LibrarySeatClient

lib = LibrarySeatClient(JACLogin(username, password))
lib.login()                                # JAccount OAuth for the seat system

zones = lib.get_zone_mapping()             # {zone_id: zone_name}
zone_info = lib.get_zone_info()            # {zone_id: ZoneInfo} (parent/child layout)
seats = lib.get_seat_ids(zone_id)          # [seat_id, ...] for a zone
seat_map = lib.get_seat_mapping()          # {seat_tag: seat_id}, e.g. "A-123" -> id

# Reserve: default window is now -> 22:30 Asia/Shanghai
success, resv_id, message = lib.reserve_seat(seat_id=1234)
success, resv_id, message = lib.reserve_seat(
    seat_id, start_time=dt, end_time=dt    # datetimes, Asia/Shanghai semantics
)
success, message = lib.cancel_reservation(resv_id)

# Query today's active reservations
for r in lib.query_reservation():          # list[ReservationInfo]
    print(r.id, r.seat_id, r.start_time, r.has_checked_in)
```

`reserve_seat` failure messages are the server's own (Chinese) strings and
encode the rules — e.g. duration must be 60–960 min, reservations open at
22:00 for the next day, one reservation per user per time window. Parse
`success`, not exceptions.

## Advanced client

`LibrarySeatAdvancedClient` adds a background scheduler + reservation cache
that auto-**postpones** your seat if you're late (no-show → infraction
avoidance):

```python
from sjtusuite.clients.library import LibrarySeatAdvancedClient

adv = LibrarySeatAdvancedClient(JACLogin(username, password))
adv.start()        # builds seat maps, starts scheduler + postpone watcher
...
adv.shutdown()
```

- `get_upcoming_reservation()` — next `ReservationInfo` (cached ~5 min).
- `seat_id_from_tag("A-123")` / `seat_tag_from_id(123)` — tag/ID conversion
  (requires `start()`).
- Postpone logic: if you haven't checked in 15 min after start, it cancels
  and re-books the same seat starting 30 min later (only if ≥1 h remains);
  the check runs every 15 min.

Gotchas:
- No CLI/daemon: to use this headless, write a small script or wrap it.
- `query_reservation()` asserts on a non-zero response code — call `login()`
  first; if session cookies are stale the API returns auth errors.
- Timezone is Asia/Shanghai; `start_time`/`end_time` are converted via
  `astimezone` — pass tz-aware datetimes to avoid surprises.
- Cookies: `data/sessions/libseat_client.cookies`.
