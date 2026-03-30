"""Checkin daemon - Flask server for automatic course checkin via polling."""
import argparse
import json
import logging
import threading
import time
from collections import deque
from datetime import datetime
from urllib.parse import parse_qs, urlparse

from flask import Flask, render_template_string, jsonify, request
from flask_cors import CORS
import requests

from sjtusuite.auth import JACLogin
from sjtusuite.clients.checkin import CheckinClient
from sjtusuite.core.credentials import credentials
from sjtusuite.notifications import DaemonNotificationClient
from sjtusuite.servers.base import get_client_ip


app = Flask(__name__)
logging.basicConfig(filename='checkin_access.log', level=logging.INFO)

CORS(app)

# Initialize JACLogin and CheckinClient
jac_login = JACLogin(credentials.username, credentials.password)
checkin_client = CheckinClient(jac_login)
notification_client = DaemonNotificationClient.from_config(
    "checkin",
    credentials.ntfy_config,
    logger=app.logger,
)

# Configuration
CONFIG = {
    'poll_url': credentials.checkin_poll_url,
    'poll_interval': credentials.checkin_poll_interval,
    'enabled': True
}

# Checkin history storage
checkin_history = deque(maxlen=50)  # Store last 50 attempts
last_successful_checkin = None
processed_timestamps = set()  # Track processed timestamps to avoid duplicates
history_lock = threading.Lock()


def build_checkin_notification(url, success, message, timestamp=None):
    """Build a human-readable notification payload for a checkin attempt."""
    parsed = urlparse(url)
    params = parse_qs(parsed.query)
    course_code = params.get('courseCode', ['unknown'])[0]
    sign_history_id = params.get('signHistoryId', ['unknown'])[0]
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


def record_checkin_attempt(url, success, message, timestamp=None):
    """Record a checkin attempt in history."""
    global last_successful_checkin
    
    attempt = {
        'url': url[:80] + '...' if len(url) > 80 else url,  # Truncate long URLs
        'success': success,
        'message': message,
        'timestamp': timestamp or datetime.now().isoformat(),
        'attempt_time': datetime.now().isoformat()
    }
    
    with history_lock:
        checkin_history.appendleft(attempt)
        if success:
            last_successful_checkin = attempt


def poll_and_checkin():
    """Background task that polls for checkin data and performs checkin."""
    app.logger.info(f"Starting poll task, URL: {CONFIG['poll_url']}, interval: {CONFIG['poll_interval']}s")
    
    while CONFIG['enabled']:
        try:
            # Fetch checkin data from configured URL
            response = requests.get(CONFIG['poll_url'], timeout=5)
            
            if response.status_code == 200:
                data = response.json()
                
                checkin_url = data.get('url')
                timestamp = data.get('timestamp')
                available = data.get('available', False)
                
                # Only process if available and not already processed
                if available and checkin_url and timestamp:
                    if timestamp not in processed_timestamps:
                        processed_timestamps.add(timestamp)
                        
                        # Keep processed_timestamps from growing too large
                        if len(processed_timestamps) > 100:
                            # Remove oldest entries (this is a set so we can't do this perfectly)
                            processed_timestamps.clear()
                            processed_timestamps.add(timestamp)
                        
                        app.logger.info(f"New checkin available: {checkin_url[:50]}...")
                        
                        # Perform checkin
                        success, message = checkin_client.checkin(checkin_url)
                        record_checkin_attempt(checkin_url, success, message, timestamp)
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
        
        time.sleep(CONFIG['poll_interval'])


# HTML template for dashboard
DASHBOARD_TEMPLATE = '''<!DOCTYPE html>
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

/* Header & Live Indicator */
header { display: flex; align-items: center; justify-content: space-between; margin-bottom: 2rem; }
h1 { font-size: 1.25rem; font-weight: 600; letter-spacing: -0.5px; }
.status-dot { 
    font-size: 0.75rem; color: var(--accent); display: flex; align-items: center; gap: 6px; 
    background: rgba(63, 185, 80, 0.1); padding: 4px 10px; border-radius: 20px;
}
.pulse { display: inline-block; width: 6px; height: 6px; background: currentColor; border-radius: 50%; animation: blink 2s infinite; }

/* Sections */
h2 { font-size: 0.8rem; text-transform: uppercase; letter-spacing: 1px; color: var(--sub-text); margin: 0 0 1rem 0; font-weight: 600; }
.section { margin-bottom: 2.5rem; }

/* Main Status Card */
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

/* History List */
.list { display: flex; flex-direction: column; gap: 1rem; }
.item { display: flex; justify-content: space-between; align-items: baseline; font-size: 0.9rem; }
.item-msg { flex: 1; margin-right: 1rem; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.item-time { color: var(--sub-text); font-size: 0.75rem; font-family: monospace; flex-shrink: 0; }
.dot { margin-right: 8px; font-weight: bold; }

/* Footer */
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
</html>'''


@app.route('/')
def dashboard():
    """Render the checkin status dashboard."""
    with history_lock:
        attempts = list(checkin_history)[:10]  # Get last 10 attempts
        last_success = last_successful_checkin
    
    return render_template_string(
        DASHBOARD_TEMPLATE,
        poll_url=CONFIG['poll_url'],
        poll_interval=CONFIG['poll_interval'],
        last_success=last_success,
        attempts=attempts
    )


@app.route('/status')
def status():
    """API endpoint to get current status as JSON."""
    with history_lock:
        attempts = list(checkin_history)[:10]
        last_success = last_successful_checkin
    
    return jsonify({
        'status': 'running',
        'config': {
            'poll_url': CONFIG['poll_url'],
            'poll_interval': CONFIG['poll_interval']
        },
        'last_successful_checkin': last_success,
        'recent_attempts': attempts,
        'total_attempts': len(checkin_history)
    })


@app.route('/history')
def history():
    """API endpoint to get full checkin history."""
    with history_lock:
        return jsonify({
            'history': list(checkin_history),
            'total': len(checkin_history)
        })


@app.after_request
def log_request(response):
    """Log all requests."""
    app.logger.info(
        f'{datetime.now()}: {request.method} {request.path} '
        f'{response.status_code} from {get_client_ip()}'
    )
    return response


def main():
    parser = argparse.ArgumentParser(description='Run Checkin Flask server with polling')
    parser.add_argument('-p', '--port', type=int, default=5002,
                        help='Port to listen on (default: 5002)')
    parser.add_argument('--poll-url', type=str, default=None,
                        help='URL to poll for checkin data')
    parser.add_argument('--poll-interval', type=float, default=None,
                        help='Polling interval in seconds')
    
    args = parser.parse_args()
    
    # Override config with command line arguments
    if args.poll_url:
        CONFIG['poll_url'] = args.poll_url
    if args.poll_interval:
        CONFIG['poll_interval'] = args.poll_interval
    
    print(f"Starting Checkin Server on port {args.port}")
    print(f"Polling URL: {CONFIG['poll_url']}")
    print(f"Polling interval: {CONFIG['poll_interval']}s")
    print(f"Dashboard: http://localhost:{args.port}/")
    
    # Start the polling thread
    poll_thread = threading.Thread(target=poll_and_checkin, daemon=True)
    poll_thread.start()
    
    app.run(debug=False, port=args.port, threaded=True)


if __name__ == '__main__':
    main()
