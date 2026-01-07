# SJTU Suite

A comprehensive collection of automation utilities for Shanghai Jiao Tong University (SJTU) services.

## Installation

```bash
# Clone the repository
git clone https://github.com/yourusername/sjtu-suite.git
cd sjtu-suite

# Create venv and install dependencies
uv sync

# Install the package
uv pip install -e .
```

## Configuration

Create a `credentials.json` file in the project root:

```json
{
    "username": "your_jaccount",
    "password": "your_password",
    "room_id": "your_dorm_room_id",
    "door_access_token": "optional_api_token"
}
```

## Quick Start

### As a Python Library

```python
from sjtusuite import JACLogin
from sjtusuite.clients import DoorClient, PEClient, VideoClient

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
```

### As Daemons (Long-Running Services)

```bash
# Door opening API server
sjtu-door -p 5000

# Auto-checkin polling daemon
sjtu-checkin -p 5002 --poll-url http://your-source/checkin-data
```

## Project Structure

```
sjtu-suite/
├── sjtusuite/            # Main package
│   ├── auth/             # JAccount OAuth authentication
│   ├── cli/              # CLI tools (rich/click)
│   │   ├── pe.py         # PE running CLI
│   │   └── video.py      # Video browser CLI
│   ├── clients/          # Service clients
│   │   ├── door.py       # Door opening
│   │   ├── pe.py         # PE running
│   │   ├── checkin.py    # Course checkin
│   │   ├── video.py      # Video download
│   │   ├── canvas.py     # Canvas LMS
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

## Environment

- Python 3.12+
- Dependencies: Flask, requests, APScheduler, flask-cors, click, rich

## License

See [LICENSE](LICENSE) for details.

## Disclaimer

This project is for educational purposes only. Use responsibly and in accordance with SJTU policies.
