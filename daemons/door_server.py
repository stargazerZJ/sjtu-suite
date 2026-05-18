"""Door opening daemon - Flask server for remote door control."""
import argparse
import logging
import hmac
import threading
import time
import uuid
from datetime import datetime

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from flask import Flask, jsonify, request

from sjtusuite.auth import JACLogin
from sjtusuite.clients.door import DoorClient
from sjtusuite.core.config import get_project_root
from sjtusuite.core.credentials import credentials
from sjtusuite.notifications import DaemonNotificationClient
from sjtusuite.servers.base import get_client_ip

app = Flask(__name__)
logging.basicConfig(filename='door_access.log', level=logging.INFO)

# Initialize DoorClient and JACLogin
jac_login = JACLogin(credentials.username, credentials.password, allow_interactive=False)
if not credentials.room_id:
    logger = logging.getLogger('door_server')
    logger.error("Room ID is not configured in credentials.")
    exit(1)
door_client = DoorClient(credentials.room_id, jac_login)
notification_client = DaemonNotificationClient.from_config(
    "door",
    credentials.ntfy_config,
    logger=app.logger,
)

# Your predefined token (ideally should be in Env Variable)
ACCESS_TOKEN = credentials.door_access_token or str(uuid.uuid4())
AUTH_FAILURE_NOTIFY_COOLDOWN_SECONDS = 60.0
auth_failure_notify_lock = threading.Lock()
last_auth_failure_notification_by_ip = {}
room_name_cache = {"value": None}


def get_room_label() -> str:
    """Return a cached room label for logs and notifications."""
    cached_room_name = room_name_cache["value"]
    if cached_room_name:
        return cached_room_name

    try:
        room_name = door_client.get_room_name()
    except Exception as exc:  # pragma: no cover - best effort metadata
        app.logger.warning("Failed to resolve room name for notification context: %s", exc)
        return credentials.room_id or "unknown room"

    if room_name:
        room_name_cache["value"] = room_name
        return room_name
    return credentials.room_id or "unknown room"


def notify_auth_failure(client_ip: str, user_agent: str) -> None:
    """Rate-limit auth failure notifications per source IP."""
    now = time.monotonic()
    should_send = False
    with auth_failure_notify_lock:
        last_sent = last_auth_failure_notification_by_ip.get(client_ip, 0.0)
        if now - last_sent >= AUTH_FAILURE_NOTIFY_COOLDOWN_SECONDS:
            last_auth_failure_notification_by_ip[client_ip] = now
            should_send = True

    if not should_send:
        return

    notification_client.notify(
        "auth_failed",
        "\n".join(
            [
                "An unauthorized door-open request was rejected.",
                f"IP: {client_ip}",
                f"User-Agent: {user_agent or 'unknown'}",
                f"Time: {datetime.now().isoformat(timespec='seconds')}",
            ]
        ),
        title="SJTU door auth failed",
        severity="error",
        tags=["access", "auth", "failed"],
    )


@app.route('/open', methods=['POST'])
def open_door():
    client_ip = get_client_ip()
    token = request.headers.get('token')
    if not token or not hmac.compare_digest(token, ACCESS_TOKEN):
        app.logger.warning(f"{datetime.now()}: Unauthorized access attempt from IP {client_ip}")
        notify_auth_failure(client_ip, request.user_agent.string)
        return jsonify({"error": "Unauthorized"}), 401

    room_label = get_room_label()
    notification_client.notify(
        "open_requested",
        "\n".join(
            [
                f"Room: {room_label}",
                f"Requested by IP: {client_ip}",
                f"Time: {datetime.now().isoformat(timespec='seconds')}",
            ]
        ),
        title="SJTU door opening requested",
        severity="info",
        tags=["access", "request"],
    )

    if door_client.open_door():
        app.logger.info(f"{datetime.now()}: Door opened successfully by IP {client_ip}")
        notification_client.notify(
            "success",
            "\n".join(
                [
                    f"Room: {room_label}",
                    f"Opened for IP: {client_ip}",
                    f"Time: {datetime.now().isoformat(timespec='seconds')}",
                ]
            ),
            title="SJTU door opened",
            severity="success",
            tags=["access", "success"],
        )
        return jsonify({"status": "Door opened successfully"}), 200
    else:
        app.logger.error(f"{datetime.now()}: Failed to open the door")
        notification_client.notify(
            "failed",
            "\n".join(
                [
                    f"Room: {room_label}",
                    f"Request IP: {client_ip}",
                    f"Time: {datetime.now().isoformat(timespec='seconds')}",
                    "The authenticated request reached the door service, but the open action failed.",
                ]
            ),
            title="SJTU door open failed",
            severity="error",
            tags=["access", "failed"],
        )
        return jsonify({"error": "Failed to open the door"}), 500


def refresh_session():
    try:
        if not door_client.login():
            app.logger.error(f"{datetime.now()}: Scheduled session refresh failed")
        else:
            app.logger.info(f"{datetime.now()}: Scheduled session refresh successful")
    except Exception as e:
        app.logger.error(f"{datetime.now()}: Scheduled session refresh failed: {e}")


@app.after_request
def log_request(response):
    if response.status_code == 404:
        app.logger.info(
            f'404: "{request.user_agent}" from IP {get_client_ip()}'
        )
    return response


# Schedule the session refresh every hour
scheduler = BackgroundScheduler(timezone="Asia/Shanghai")
cron_trigger = CronTrigger(timezone="Asia/Shanghai", minute='0,30')
scheduler.add_job(refresh_session, trigger=cron_trigger)


def main():
    parser = argparse.ArgumentParser(description='Run Door Flask app with specified port.')
    parser.add_argument('-p', '--port', type=int, default=5000, help='Port to listen on (default: 5000)')

    door_client.login()
    scheduler.start()
    args = parser.parse_args()
    app.run(debug=False, port=args.port)


if __name__ == '__main__':
    main()
