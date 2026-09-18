# Lecture video download (Canvas + video portal)

Download lecture recordings from SJTU's Canvas-integrated video system.
Three services are chained: Canvas (oc.sjtu.edu.cn, JAccount OAuth) → video
portal (v.sjtu.edu.cn, auth via tokenId) → video CDN (live.sjtu.edu.cn,
temporary-key MP4 URLs). Modules: `sjtusuite/clients/canvas.py`
(`CanvasClient`), `sjtusuite/clients/video.py` (`VideoClient`).

## CLI

```bash
uv run sjtu-video                          # fully interactive course picker
uv run sjtu-video -c 12345                 # preselect a Canvas course by ID
uv run sjtu-video -o ./videos -p 5         # output dir + parallel downloads
uv run sjtu-video -s                       # subtitles only, non-interactive
```

The picker is interactive (rich Prompts) — agents should use the Python API
instead unless driving it for a user. `-s` (subtitles-only) is the one
non-interactive mode.

## Python API

```python
from sjtusuite import JACLogin
from sjtusuite.clients import CanvasClient, VideoClient

jac = JACLogin(username, password)
canvas = CanvasClient(jac)
video = VideoClient(jac)

canvas.login()
courses = canvas.get_courses_with_video()          # only courses with a video tool
video_url = canvas.get_course_video_url(course_id) # the external-tool URL

video.login(video_url)                  # walks the chain, stores tokenId
sessions = video.get_sessions()         # lecture sessions (newest first)

video.download_session(sessions[0], output_dir="./videos")

# Extras
summary = video.get_course_summary()    # AI summary per lecture segment
subs = video.get_subtitles()            # transcript data
srt_text = video.format_subtitles_srt(subs)
txt = video.format_summary_txt(summary)
```

Flow requirements:
- Always `canvas.login()` first, then `canvas.get_course_video_url(...)` →
  `video.login(url)`. `list_sessions(course_url)` does login+list in one
  call if you only need the session list.
- `get_sessions()` uses the Canvas course by default; `video_course_id` (the
  video-platform ID) gets set as a side effect and is needed for
  summary/subtitles.
- `download_video` streams to disk with a `progress_callback(downloaded,
  total)` hook — use it instead of polling files.
- Videos have multiple channels (camera/screen); `get_video_urls(info,
  channels)` and `download_session(..., channels=[0])` control which.
  Default downloads all channels as `<name>_chN.mp4`.

Gotchas:
- CDN URLs carry a temporary key — download promptly after fetching
  `get_video_info`, don't cache URLs across runs.
- Session cookies: `data/sessions/canvas_client.cookies` and
  `video_client.cookies`. The video `login()` must re-run when tokenId
  expires; it is *not* inferable from the cookie jar alone.
- A course without the video external tool won't appear in
  `get_courses_with_video()` — use `canvas.get_courses()` to see everything.
