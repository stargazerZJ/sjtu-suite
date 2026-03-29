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
    "door_access_token": "optional_api_token"
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
sjtu-sports venues
sjtu-sports --from-browser venues         # Reuse the current Playwright browser login
sjtu-sports availability <venue_id> --motion 乒乓球
sjtu-sports reserve <venue_id> --motion 乒乓球 --date 2026-03-29 --field 场地10 --time 19:00-20:00

# Sports reservation daemon dashboard
sjtu-sportsd
sjtu-sportsd --from-browser               # Test the daemon against the current Playwright browser login
```

### As Daemons (Long-Running Services)

```bash
# Door opening API server
sjtu-door -p 5000

# Auto-checkin polling daemon
sjtu-checkin -p 5002 --poll-url http://your-source/checkin-data

# Sports reservation daemon
sjtu-sportsd -p 5003
```

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
│   │   └── library/      # Library seat reservation
│   ├── core/             # Utilities (logging, config)
│   └── servers/          # Flask server utilities
├── daemons/              # Long-running services
│   ├── door_server.py
│   └── checkin_server.py
├── data/                 # Runtime data (cookies, logs)
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

If you have already logged in through the managed Playwright browser, the sports CLI can reuse that authenticated browser session instead of `credentials.json`:

```bash
sjtu-sports --from-browser detail <venue_id>
sjtu-sports --from-browser reserve <venue_id> --motion 乒乓球 --date 2026-03-30 --field 场地2 --time 12:00-13:00
```

The daemon exposes a web dashboard where you can create reservation jobs, inspect recent attempts, and manually trigger a dry run or a real booking attempt:

```bash
sjtu-sportsd -p 5003
# open http://localhost:5003/
```

By default the daemon uses `credentials.json` for long-running login refresh. `sjtu-sportsd --from-browser` is also supported for live testing while the managed Playwright browser is logged in.

## Environment

- Python 3.12+
- Dependencies: Flask, requests, APScheduler, flask-cors, click, rich, pycryptodome

## License

See [LICENSE](LICENSE) for details.

## Disclaimer

This project is for educational purposes only. Use responsibly and in accordance with SJTU policies.
