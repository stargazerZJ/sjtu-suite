"""Checkin daemon - Flask server for automatic course checkin via polling."""
import argparse
import json
import logging
import threading
import time
from collections import deque
from datetime import datetime
from urllib.parse import parse_qs, urlparse

import requests
from flask import Flask, jsonify, render_template_string, request
from flask_cors import CORS

from sjtusuite.auth import JACLogin
from sjtusuite.clients.checkin import CheckinClient
from sjtusuite.notifications import DaemonNotificationClient
from sjtusuite.servers.base import get_client_ip


def build_checkin_notification(url, success, message, timestamp=None):
    """Build a human-readable notification payload for a checkin attempt."""
    parsed = urlparse(url)
    params = parse_qs(parsed.query)
    course_code = params.get("courseCode", ["unknown"])[0]
    sign_history_id = params.get("signHistoryId", ["unknown"])[0]
    status_label = "succeeded" if success else "failed"
    title = f"SJTU checkin {status_label}"
    body_lines = [
        f"Status: {status_label}",
        f"Course: {course_code}",
        f"Sign history: {sign_history_id}",
        f"Timestamp: {timestamp or 'unknown'}",
        f"Result: {message}",
    ]
    return {
        "title": title,
        "message": "\n".join(body_lines),
        "event": "success" if success else "failed",
        "severity": "success" if success else "error",
        "tags": ["attendance", "success" if success else "failed"],
    }


def record_checkin_attempt(state, url, success, message, timestamp=None):
    """Record a checkin attempt in app-local history."""
    attempt = {
        "url": url[:80] + "..." if len(url) > 80 else url,
        "success": success,
        "message": message,
        "timestamp": timestamp or datetime.now().isoformat(),
        "attempt_time": datetime.now().isoformat(),
    }

    with state["lock"]:
        state["history"].appendleft(attempt)
        if success:
            state["last_successful_checkin"] = attempt


def poll_and_checkin(app, config, state, checkin_client, notification_client):
    """Background task that polls for checkin data and performs checkin."""
    app.logger.info(f"Starting poll task, URL: {config['poll_url']}, interval: {config['poll_interval']}s")

    while config["enabled"]:
        try:
            response = requests.get(config["poll_url"], timeout=5)

            if response.status_code == 200:
                data = response.json()

                checkin_url = data.get("url")
                timestamp = data.get("timestamp")
                available = data.get("available", False)

                if available and checkin_url and timestamp:
                    processed_timestamps = state["processed_timestamps"]
                    if timestamp not in processed_timestamps:
                        processed_timestamps.add(timestamp)

                        if len(processed_timestamps) > 100:
                            processed_timestamps.clear()
                            processed_timestamps.add(timestamp)

                        app.logger.info(f"New checkin available: {checkin_url[:50]}...")

                        success, message = checkin_client.checkin(checkin_url)
                        record_checkin_attempt(state, checkin_url, success, message, timestamp)
                        notification = build_checkin_notification(
                            checkin_url,
                            success,
                            message,
                            timestamp,
                        )
                        notification_client.notify(
                            notification["event"],
                            notification["message"],
                            title=notification["title"],
                            severity=notification["severity"],
                            tags=notification["tags"],
                        )

                        app.logger.info(f"Checkin result: success={success}, message={message}")

        except requests.exceptions.RequestException as e:
            app.logger.debug(f"Poll request failed: {e}")
        except json.JSONDecodeError as e:
            app.logger.warning(f"Invalid JSON response: {e}")
        except Exception as e:
            app.logger.error(f"Poll error: {e}")

        time.sleep(config["poll_interval"])


DASHBOARD_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>签到监控</title>
<style>
:root {
    --bg: #0a0a0f;
    --card-bg: #1a1a1a;
    --text: #eee;
    --sub-text: #888;
    --accent: #3fb950;
    --error: #f85149;
    --font: -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif;
}
* { margin: 0; padding: 0; box-sizing: border-box; }
body {
    font-family: var(--font);
    background: var(--bg);
    color: var(--text);
    padding: 2rem 1rem;
    line-height: 1.5;
}
.wrap { max-width: 480px; margin: 0 auto; }

header { display: flex; align-items: center; justify-content: space-between; margin-bottom: 2rem; }
h1 { font-size: 1.25rem; font-weight: 600; letter-spacing: -0.5px; }
.status-dot {
    font-size: 0.75rem; color: var(--accent); display: flex; align-items: center; gap: 6px;
    background: rgba(63, 185, 80, 0.1); padding: 4px 10px; border-radius: 20px;
}
.pulse { display: inline-block; width: 6px; height: 6px; background: currentColor; border-radius: 50%; animation: blink 2s infinite; }

h2 { font-size: 0.8rem; text-transform: uppercase; letter-spacing: 1px; color: var(--sub-text); margin: 0 0 1rem 0; font-weight: 600; }
.section { margin-bottom: 2.5rem; }

.hero {
    background: var(--card-bg);
    padding: 1.5rem;
    border-radius: 12px;
    text-align: center;
}
.hero-status { font-size: 1.5rem; margin-bottom: 0.5rem; font-weight: bold; }
.hero-time { color: var(--sub-text); font-size: 0.85rem; font-family: monospace; }
.st-ok { color: var(--accent); }
.st-fail { color: var(--error); }

.list { display: flex; flex-direction: column; gap: 1rem; }
.item { display: flex; justify-content: space-between; align-items: baseline; font-size: 0.9rem; }
.item-msg { flex: 1; margin-right: 1rem; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.item-time { color: var(--sub-text); font-size: 0.75rem; font-family: monospace; flex-shrink: 0; }
.dot { margin-right: 8px; font-weight: bold; }

.meta {
    border-top: 1px solid #333;
    padding-top: 1.5rem;
    font-size: 0.75rem;
    color: var(--sub-text);
    display: flex;
    justify-content: space-between;
}
.empty { color: var(--sub-text); font-style: italic; font-size: 0.9rem; }

@keyframes blink { 0% { opacity: 1; } 50% { opacity: 0.4; } 100% { opacity: 1; } }
</style>
</head>
<body>
<div class="wrap">
    <header>
        <h1>签到监控</h1>
        <div class="status-dot"><span class="pulse"></span> 查询间隔 {{poll_interval}}s</div>
    </header>

    <div class="section">
        <h2>最新状态</h2>
        {% if last_success %}
        <div class="hero">
            <div class="hero-status st-ok">{{last_success.message}}</div>
            <div class="hero-time">{{last_success.attempt_time[:19]}}</div>
        </div>
        {% else %}
        <div class="hero"><div class="hero-status" style="color:#666">等待数据...</div></div>
        {% endif %}
    </div>

    <div class="section">
        <h2>最近记录 ({{attempts|length}})</h2>
        <div class="list">
            {% if attempts %}
                {% for a in attempts %}
                <div class="item">
                    <div class="item-msg">
                        <span class="dot {{'st-ok' if a.success else 'st-fail'}}">
                            {{'•' if a.success else '×'}}
                        </span>
                        <span style="{{'' if a.success else 'color:var(--error)'}}">{{a.message}}</span>
                    </div>
                    <div class="item-time">{{a.attempt_time[11:19]}}</div>
                </div>
                {% endfor %}
            {% else %}
                <div class="empty">暂无历史记录</div>
            {% endif %}
        </div>
    </div>

    <div class="meta">
        <div>URL: {{poll_url}}</div>
        <div>自动刷新</div>
    </div>
</div>
<script>setTimeout(()=>location.reload(),10000)</script>
</body>
</html>"""


def create_app(*, poll_url=None, poll_interval=None, credential_provider=None) -> Flask:
    """Create the checkin dashboard app without starting the polling thread."""
    if credential_provider is None:
        from sjtusuite.core.credentials import credentials as credential_provider

    app = Flask(__name__)
    CORS(app)

    jac_login = JACLogin(
        credential_provider.username,
        credential_provider.password,
        allow_interactive=False,
    )
    checkin_client = CheckinClient(jac_login)
    notification_client = DaemonNotificationClient.from_config(
        "checkin",
        credential_provider.ntfy_config,
        logger=app.logger,
    )
    config = {
        "poll_url": poll_url or credential_provider.checkin_poll_url,
        "poll_interval": poll_interval or credential_provider.checkin_poll_interval,
        "enabled": True,
    }
    state = {
        "history": deque(maxlen=50),
        "last_successful_checkin": None,
        "processed_timestamps": set(),
        "lock": threading.Lock(),
    }

    @app.route("/")
    def dashboard():
        """Render the checkin status dashboard."""
        with state["lock"]:
            attempts = list(state["history"])[:10]
            last_success = state["last_successful_checkin"]

        return render_template_string(
            DASHBOARD_TEMPLATE,
            poll_url=config["poll_url"],
            poll_interval=config["poll_interval"],
            last_success=last_success,
            attempts=attempts,
        )

    @app.route("/status")
    def status():
        """API endpoint to get current status as JSON."""
        with state["lock"]:
            attempts = list(state["history"])[:10]
            last_success = state["last_successful_checkin"]

        return jsonify(
            {
                "status": "running",
                "config": {
                    "poll_url": config["poll_url"],
                    "poll_interval": config["poll_interval"],
                },
                "last_successful_checkin": last_success,
                "recent_attempts": attempts,
                "total_attempts": len(state["history"]),
            }
        )

    @app.route("/history")
    def history():
        """API endpoint to get full checkin history."""
        with state["lock"]:
            return jsonify(
                {
                    "history": list(state["history"]),
                    "total": len(state["history"]),
                }
            )

    @app.after_request
    def log_request(response):
        """Log all requests."""
        app.logger.info(
            f"{datetime.now()}: {request.method} {request.path} "
            f"{response.status_code} from {get_client_ip()}"
        )
        return response

    app.config["CHECKIN_CONFIG"] = config
    app.config["CHECKIN_STATE"] = state
    app.config["POLL_TARGET"] = lambda: poll_and_checkin(
        app,
        config,
        state,
        checkin_client,
        notification_client,
    )
    return app


def main():
    parser = argparse.ArgumentParser(description="Run Checkin Flask server with polling")
    parser.add_argument("-p", "--port", type=int, default=5002, help="Port to listen on (default: 5002)")
    parser.add_argument("--poll-url", type=str, default=None, help="URL to poll for checkin data")
    parser.add_argument("--poll-interval", type=float, default=None, help="Polling interval in seconds")
    args = parser.parse_args()

    logging.basicConfig(filename="checkin_access.log", level=logging.INFO)
    app = create_app(poll_url=args.poll_url, poll_interval=args.poll_interval)
    config = app.config["CHECKIN_CONFIG"]

    print(f"Starting Checkin Server on port {args.port}")
    print(f"Polling URL: {config['poll_url']}")
    print(f"Polling interval: {config['poll_interval']}s")
    print(f"Dashboard: http://localhost:{args.port}/")

    poll_thread = threading.Thread(target=app.config["POLL_TARGET"], daemon=True)
    poll_thread.start()

    app.run(debug=False, port=args.port, threaded=True)


if __name__ == "__main__":
    main()
