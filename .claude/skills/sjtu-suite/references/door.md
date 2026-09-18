# Dorm door control

Open SJTU dormitory doors remotely via door.sjtu.edu.cn. Module:
`sjtusuite/clients/door.py` (`DoorClient`, `get_truncated_room_id`).
For headless/remote use, run the `sjtu-door` daemon and POST to it (see
[daemons.md](daemons.md)).

## Python API

```python
from sjtusuite import JACLogin
from sjtusuite.clients import DoorClient

door = DoorClient(room_id, JACLogin(username, password))
door.login()
door.open_door()                # True on success
name = door.get_room_name()     # "闵行校区-东X区-xxxx" or an error string
```

- `room_id` is the full ID from the door system; the API only uses the
  prefix (`get_truncated_room_id` strips the trailing 8 digits, which encode
  a date). Both methods accept an optional `room_id` override argument.
- `open_door` auto-retries once through `login()` on a 401.
- Error surfaces: JSON body `errno` 403 = "no permission for this door"
  (wrong room or no authorization); `get_room_name` returning
  `"房间号输入错误！"` means bad room id.

## One-off from the shell

```bash
uv run python -c "
from sjtusuite.core.credentials import credentials
from sjtusuite.auth import JACLogin
from sjtusuite.clients import DoorClient
c = credentials
DoorClient(c.room_id, JACLogin(c.username, c.password)).open_door() and print('opened')
"
```

(`room_id` must be set in `credentials.json`.)

Gotchas:
- Each open request is a real remote unlock of a physical door — only do it
  on the user's request.
- Cookies: `data/sessions/door_client.cookies`.
