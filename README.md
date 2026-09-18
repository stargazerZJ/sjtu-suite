# SJTU Suite

A comprehensive collection of automation utilities for Shanghai Jiao Tong University (SJTU) services.

## Installation

```bash
# Clone the repository
git clone https://github.com/stargazerZJ/sjtu-suite.git
cd sjtu-suite

# Create venv and install dependencies
uv sync

# Install the package
uv pip install -e .
```

## Configuration

Create a `credentials.json` file in the project root. You can start from `credentials.json.example`. You are encouraged to use environment variables for sensitive data like passwords.

```json
{
    "username": "your_jaccount",
    "password": {
        "mode": "env",
        "key": "JACCOUNT_PASSWORD"
    },
    "room_id": "your_dorm_room_id",
    "door_access_token": "optional_api_token",
    "notifications": {
        "ntfy": {
            "enabled": true,
            "server": "https://ntfy.sh",
            "topic": "your-unique-topic",
            "token": {
                "mode": "env",
                "key": "NTFY_TOKEN"
            },
            "tags": ["sjtu", "daemon"],
            "priority": "high"
        }
    }
}
```

If you prefer to store the password directly in the file (not recommended), use:

```json
{
    ...
    "password": {
        "mode": "literal",
        "value": "your_password_here"
    }
}
```

`notifications.ntfy` is optional. When configured, the daemons reuse the same `ntfy.sh` topic for operational push notifications:

- `sjtu-sportsd`: successful sports reservation orders
- `sjtu-checkin`: fresh check-in attempts that succeed or fail
- `sjtu-door`: open request received, open success/failure, and unauthorized access rejections

Door auth-failure notifications are rate-limited to at most one push per source IP per minute to avoid alert storms.

`notifications.mail` is also optional and enables SMTP mail notifications via `mail.sjtu.edu.cn` (see below). It reuses the JAccount credentials by default, so a minimal block is just `"to": ["your_name@sjtu.edu.cn"]`.

## Quick Start

### As a Python Library

```python
from sjtusuite import JACLogin
from sjtusuite.clients import DoorClient, PEClient, SportsReservationClient, VideoClient

# Initialize authentication
jac_login = JACLogin(username="xxx", password="xxx")

# Use door client
door = DoorClient(room_id="xxx", jac_login=jac_login)
door.login()
door.open_door()

# Use PE client
pe = PEClient(jac_login)
pe.simulate_running()

# Use video client
video = VideoClient(jac_login)
sessions = video.list_sessions(course_url)

# Use sports client
sports = SportsReservationClient(jac_login)
sports.login()
venues, total = sports.list_venues(venue_name="南区")
```

### As CLI Tools

After installation, the following commands become available:

```bash
# Interactive Canvas video downloader
sjtu-video

# PE running simulation
sjtu-pe run                    # Default 2km run
sjtu-pe run -d 3.0             # Custom distance
sjtu-pe run --dry-run          # Preview without uploading
sjtu-pe status                 # Check login status

# Sports venue reservation
sjtu-sports                              # Interactive venue -> motion -> date -> slot picker
sjtu-sports venues                       # Fetch all venue pages by default
sjtu-sports venues --page-num 2          # Inspect a single page when needed
sjtu-sports availability <venue_id> --motion 乒乓球
sjtu-sports reserve <venue_id> --motion 乒乓球 --date 2026-03-29 --field 场地10 --time 19:00-20:00

# Sports reservation daemon dashboard
sjtu-sportsd
sjtu-sportsd --host 0.0.0.0 -p 5003      # Expose the dashboard on all network interfaces

# University mailbox (mail.sjtu.edu.cn, IMAP/SMTP)
sjtu-mail folders                          # Folder list with unread counts
sjtu-mail list -n 20                       # Newest messages in INBOX
sjtu-mail list --folder Sent --unread      # Unread messages in another folder
sjtu-mail read 2046 --mark-read            # Read a message (and mark it seen)
sjtu-mail mark 2046 --unread               # Mark read/unread
sjtu-mail send -t name@sjtu.edu.cn -s "Hi" -b "Body"
sjtu-mail test                             # Send a self-test mail
```

The sports daemon supports two job modes:

- `target_date`: a noon-opening watcher for a specific reservation date; the dashboard now lets you choose which day's `12:00` should trigger the booking attempt, defaulting to the first upcoming noon
- `cron`: a cancellation watcher that scans every `N` minutes for free slots inside a configurable date window and only books slots that are still before the redeem cutoff (default `2` hours before start)

### As Daemons (Long-Running Services)

```bash
# Door opening API server
sjtu-door -p 5000

# Auto-checkin polling daemon
sjtu-checkin -p 5002 --poll-url http://your-source/checkin-data

# Sports reservation daemon
sjtu-sportsd -p 5003
sjtu-sportsd --host 0.0.0.0 -p 5003
```

## Agent Skills

This repository ships Claude Code skills under `.claude/skills/`:

- `sjtu-suite` — how to *use* the suite (CLIs, Python API, daemon REST APIs), with per-component reference files under `references/`
- `sjtu-suite-dev` — how to *develop* on the codebase (architecture, conventions, how to add a client or daemon)

Point any coding agent at this repo and it can pick these up automatically.

## Project Structure

```
sjtu-suite/
├── sjtusuite/            # Main package
│   ├── auth/             # JAccount OAuth authentication
│   ├── cli/              # CLI tools (rich/click)
│   │   ├── pe.py         # PE running CLI
│   │   ├── sports.py     # Sports reservation CLI
│   │   └── video.py      # Video browser CLI
│   ├── clients/          # Service clients
│   │   ├── door.py       # Door opening
│   │   ├── pe.py         # PE running
│   │   ├── checkin.py    # Course checkin
│   │   ├── video.py      # Video download
│   │   ├── canvas.py     # Canvas LMS
│   │   ├── sports.py     # Sports reservation
│   │   ├── questionnaire.py # Questionnaire service
│   │   ├── mail.py       # University mailbox (IMAP/SMTP)
│   │   └── library/      # Library seat reservation
│   ├── core/             # Utilities (logging, config)
│   ├── data/             # Packaged data used by clients
│   ├── notifications/    # Reusable notification integrations
│   └── servers/          # Flask server utilities
├── daemons/              # Long-running services
│   ├── door_server.py
│   └── checkin_server.py
├── data/                 # Runtime data (cookies, logs, daemon state)
└── credentials.json      # User credentials (gitignored)
```

## Service Clients

| Client | Description |
|--------|-------------|
| `DoorClient` | Open dormitory doors remotely |
| `PEClient` | Submit simulated running records |
| `CheckinClient` | Handle course attendance checkins |
| `VideoClient` | Download lecture video recordings |
| `CanvasClient` | Access Canvas LMS courses and tools |
| `LibrarySeatClient` | Reserve and manage library seats |
| `SportsReservationClient` | Discover, preview, and submit sports venue reservations |
| `QuestionnaireClient` | List questionnaires and fetch submissions from wj.sjtu.edu.cn |
| `MailClient` | Read and send mail on mail.sjtu.edu.cn via IMAP/SMTP |

## API Examples

### Door Client

```python
from sjtusuite.clients import DoorClient
from sjtusuite import JACLogin

jac = JACLogin("user", "pass")
door = DoorClient("room_id", jac)

door.login()
door.open_door()
room_name = door.get_room_name()
```

### Library Seat Client

```python
from sjtusuite.clients.library import LibrarySeatClient
from sjtusuite import JACLogin

jac = JACLogin("user", "pass")
lib = LibrarySeatClient(jac)

zones = lib.get_zone_mapping()
success, id, msg = lib.reserve_seat(seat_id=1234)
lib.cancel_reservation(id)
```

### Video Download

```python
from sjtusuite.clients import VideoClient, CanvasClient
from sjtusuite import JACLogin

jac = JACLogin("user", "pass")
canvas = CanvasClient(jac)
video = VideoClient(jac)

# Get courses
courses = canvas.get_courses_with_video()

# Get video sessions
video_url = canvas.get_course_video_url(course_id)
video.login(video_url)
sessions = video.get_sessions()

# Download
video.download_session(sessions[0], output_dir="./videos")
```

### Sports Reservation

```python
from sjtusuite.clients import SportsReservationClient
from sjtusuite import JACLogin

jac = JACLogin("user", "pass")
sports = SportsReservationClient(jac)

sports.login()
venues, total = sports.list_venues(venue_name="南区")
preview = sports.preview_personal_order(
    venue_id=venues[0].venue_id,
    motion="乒乓球",
    date="2026-03-29",
    field_names=["场地10"],
    time_slots=["19:00-20:00"],
)
```

### University Mail

```python
from sjtusuite.clients import MailClient
from sjtusuite import JACLogin  # mail uses the same credentials directly

mail = MailClient("user", "pass")  # same JAccount credentials as everywhere else

# Read
mail.list_folders()
mail.unread_count()                      # INBOX unread
mail.list_messages(limit=20)             # newest-first envelopes
body = mail.get_body(uid)                # full text of one message
mail.mark_read(uid)                      # flag management

# Send
mail.send(to="someone@sjtu.edu.cn", subject="Hi", body="...")

# As a notification channel (best-effort, never raises)
from sjtusuite.notifications import MailNotifier

notifier = MailNotifier(MailClient("user", "pass"), to="me@sjtu.edu.cn")
result = notifier.send("Reservation confirmed", subject="Sports booked")
```

The `notifications.mail` config block in `credentials.json` builds the same
notifier from configuration for daemon reuse.

### Questionnaire Service

```python
from sjtusuite.clients import QuestionnaireClient
from sjtusuite import JACLogin

jac = JACLogin("user", "pass")
wj = QuestionnaireClient(jac)
wj.login()

for q in wj.list_questionnaires(include_archived=False):
    print(q.questionnaire_id, q.name)

# All submissions of one questionnaire (auto-paginated), answers keyed by question title
sheets = wj.list_submissions(117406)
for sheet in sheets:
    print(sheet.user_name, sheet.finish_at, sheet.answers)

# Or page through the raw table rows yourself
rows, total = wj.list_submission_rows(117406, page=1, page_size=20)

# Full detail of a single submission (answersheet id from a row/sheet)
detail = wj.get_submission(sheets[0].answersheet_id)
```

The daemon exposes a web dashboard where you can create reservation jobs, inspect recent attempts, and manually trigger a dry run or a real booking attempt:

```bash
sjtu-sportsd -p 5003
# open http://localhost:5003/
sjtu-sportsd --host 0.0.0.0 -p 5003
# open http://<server-ip>:5003/ from another device on the same network
```

By default the daemon binds to `127.0.0.1` and uses `credentials.json` for long-running login refresh. Daemon authentication is non-interactive: if JAccount requires two-step verification or manual captcha entry, the daemon reports a clear authentication error instead of waiting for terminal input. In `target_date` mode, new dashboard jobs default to the first upcoming noon, but you can override the exact run date if you want a later `12:00` trigger. Use `--host` if you want a different bind address.

## Environment

- Python 3.12+
- Dependencies: Flask, requests, APScheduler, flask-cors, click, rich, pycryptodome

## License

See [LICENSE](LICENSE) for details.

## Disclaimer

This project is for educational purposes only. Use responsibly and in accordance with SJTU policies.
